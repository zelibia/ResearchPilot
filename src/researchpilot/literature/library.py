"""文献库：一篇文献在库里只有一份"身份"，可以被多个项目引用。

为什么要有这一层：
项目是**研究主题**，文献是**资料**。同一篇 Bensen 2007 完全可能同时出现在
两个项目里——用户不该为了在第二个项目里引用它而重新上传一遍，也不该因为
"另一个项目里有"就被告知"重复、不入库"然后什么都看不到。

身份怎么判定（顺序固定，越靠前越可靠）：

1. ``sha256``：同一份文件内容，最硬的依据；
2. ``doi``（规范化、大小写不敏感）：同一篇文献的另一份文件；
3. 都没有时退到记录 id——这种记录只能存在于自己的项目里，不参与跨项目合并。

项目内仍然是**一条 Paper 记录**（``project_id`` 指向自己的项目），
所以项目隔离、关系裁决、计划证据、导出这些既有逻辑一条都不用改。
"加入本项目"在这里做的是：把库里已有的元数据与文件引用复制成目标项目的
一条记录，不重新解析、不重复占用磁盘。

本模块只读写数据库与 uploads 目录，不调用模型、不联网。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..storage import repository as repo
from ..storage.models import (
    Method,
    Paper,
    PaperClaim,
    PaperRelation,
    PlanDraft,
    PlanVersion,
    Project,
    SourceRecord,
)
from . import paths as paper_paths
from .normalize import normalize_doi

log = logging.getLogger(__name__)

# 从库里加入本项目的记录，来源标记与"用户上传"分开：
# 报告与目录里要能看出"这篇是资料库复用来的"。
ORIGIN_LIBRARY = "library"


def identity_key(paper: Paper) -> str:
    """一篇文献的库身份。同一身份的记录在库里视为同一篇文献。"""
    if paper.sha256:
        return f"sha256:{paper.sha256}"
    doi = normalize_doi(paper.doi)
    if doi:
        return f"doi:{doi}"
    return f"id:{paper.id}"


@dataclass
class LibraryEntry:
    """库里的一篇文献：一份身份 + 引用它的全部记录。"""

    key: str
    paper: Paper
    members: list[Paper] = field(default_factory=list)

    @property
    def project_ids(self) -> list[str]:
        """引用这篇文献的项目（去重、保持稳定顺序）。"""
        seen: list[str] = []
        for member in self.members:
            if member.project_id not in seen:
                seen.append(member.project_id)
        return seen

    def in_project(self, project_id: str) -> Paper | None:
        for member in self.members:
            if member.project_id == project_id:
                return member
        return None

    @property
    def file_available(self) -> bool:
        return paper_paths.resolve_local_path(self.paper.local_path) is not None


def entries() -> list[LibraryEntry]:
    """全库清单，按身份合并；代表记录取最早入库的那一条。"""
    grouped: dict[str, list[Paper]] = {}
    for paper in repo.list_all(Paper):
        grouped.setdefault(identity_key(paper), []).append(paper)

    result: list[LibraryEntry] = []
    for key, members in grouped.items():
        ordered = sorted(members, key=lambda item: (str(item.created_at or ""), item.id))
        result.append(LibraryEntry(key=key, paper=ordered[0], members=ordered))
    result.sort(key=lambda entry: (str(entry.paper.created_at or ""), entry.paper.id), reverse=True)
    return result


def entry_for(paper: Paper) -> LibraryEntry | None:
    key = identity_key(paper)
    for entry in entries():
        if entry.key == key:
            return entry
    return None


def find_in_other_projects(project_id: str, *, sha256: str = "", doi: str = "") -> Paper | None:
    """在**其它**项目里找同一篇文献。返回那条记录，找不到返回 None。

    传入 sha256 与 doi 任一即可；两个都给时 sha256 优先（文件内容是硬依据）。
    这是"上传时命中库"的判定点，与项目内去重（``ingest``）是两件事。
    """
    if sha256:
        for paper in repo.list_all(Paper):
            if paper.project_id != project_id and paper.sha256 == sha256:
                return paper
    target = normalize_doi(doi)
    if target:
        for paper in repo.list_all(Paper):
            if paper.project_id != project_id and paper.doi and normalize_doi(paper.doi) == target:
                return paper
    return None


def attach_to_project(project_id: str, source: Paper) -> tuple[Paper, bool]:
    """把库里已有的文献加入另一个项目。

    第二个返回值表示"本次是否真的新增"。目标项目里已经有同一篇（按身份判定）时
    返回已有记录，不复制第二份。
    """
    existing = [
        paper
        for paper in repo.list_all(Paper, project_id)
        if identity_key(paper) == identity_key(source)
    ]
    if existing:
        return existing[0], False

    clone = source.model_copy(
        update={
            "id": repo.new_id("paper"),
            "project_id": project_id,
            "origin": ORIGIN_LIBRARY,
            "created_at": repo.now_iso(),
        }
    )
    # 同一份文件不重复拷贝：直接复用原来的 local_path 与 sha256。
    repo.save(clone)
    log.info(
        "文献库复用：%s（来自 %s）加入项目 %s",
        clone.id,
        source.project_id,
        project_id,
    )
    return clone, True


def adopt_candidate(project_id: str, paper_id: str) -> tuple[Paper, bool]:
    """Import an OpenAlex candidate into the user's project library.

    Candidate metadata is kept unchanged. Only its library membership changes,
    so adopting it never rewrites title, authors, year, DOI or source records.
    """
    paper = repo.get(Paper, paper_id)
    if paper is None or paper.project_id != project_id:
        raise ValueError("候选文献不属于当前项目")
    if paper.origin != "openalex":
        return paper, False
    paper.origin = ORIGIN_LIBRARY
    repo.save(paper)
    log.info("候选文献导入项目文献库：%s（项目 %s）", paper.id, project_id)
    return paper, True


def _clean_categories(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not value:
            continue
        value = value[:30]
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= 12:
            break
    return result


def categories_in_project(project_id: str) -> list[str]:
    names = {
        category
        for paper in repo.list_all(Paper, project_id)
        for category in (paper.library_categories or [])
        if category
    }
    return sorted(names, key=lambda item: item.casefold())


def organize_papers(
    project_id: str,
    paper_ids: list[str],
    *,
    replace_categories: list[str] | None = None,
    add_categories: list[str] | None = None,
    remove_categories: list[str] | None = None,
    note: str | None = None,
) -> int:
    """Apply user-named collection categories without touching files or claims.

    Categories behave like user-created folders or favourites. They are library
    metadata only and never rename, move, rewrite or delete the paper itself.
    """
    targets: list[Paper] = []
    for paper_id in dict.fromkeys(paper_ids):
        paper = repo.get(Paper, paper_id)
        if paper is None or paper.project_id != project_id:
            raise ValueError("选中的文献不属于当前项目")
        targets.append(paper)
    if not targets:
        return 0

    add = _clean_categories(add_categories or [])
    remove = {item.casefold() for item in _clean_categories(remove_categories or [])}
    replace = (
        _clean_categories(replace_categories)
        if replace_categories is not None
        else None
    )
    for paper in targets:
        categories = (
            list(replace)
            if replace is not None
            else list(paper.library_categories or [])
        )
        if remove:
            categories = [
                item for item in categories if item.casefold() not in remove
            ]
        if add:
            categories = _clean_categories([*categories, *add])
        paper.library_categories = categories
        if note is not None:
            paper.library_note = str(note).strip()[:1000]
        repo.save(paper)
    log.info(
        "批量整理文献 project=%s count=%d add=%s remove=%s replace=%s note=%s",
        project_id, len(targets), add, sorted(remove), replace is not None, note is not None,
    )
    return len(targets)


def move_to_project(
    project_id: str,
    paper_ids: list[str],
    target_project_id: str,
) -> dict[str, int]:
    """Move selected project records to another project, reusing their files."""
    if project_id == target_project_id:
        raise ValueError("目标项目不能是当前项目")
    target = repo.get(Project, target_project_id)
    if target is None:
        raise ValueError("目标项目不存在")
    moved = 0
    already_there = 0
    for paper_id in dict.fromkeys(paper_ids):
        paper = repo.get(Paper, paper_id)
        if paper is None or paper.project_id != project_id:
            raise ValueError("选中的文献不属于当前项目")
        _, created = attach_to_project(target_project_id, paper)
        if not created:
            already_there += 1
        remove_paper(paper, delete_file=False)
        moved += 1
    log.info(
        "批量移动文献 from=%s to=%s moved=%d already_there=%d",
        project_id, target_project_id, moved, already_there,
    )
    return {"moved": moved, "already_there": already_there}


def remove_many(
    project_id: str,
    paper_ids: list[str],
    *,
    delete_file: bool = False,
) -> dict[str, int]:
    """Remove selected records from one project and clean derived records."""
    totals = {
        "records": 0,
        "relations": 0,
        "claims": 0,
        "plan_evidence": 0,
        "files_deleted": 0,
    }
    for paper_id in dict.fromkeys(paper_ids):
        paper = repo.get(Paper, paper_id)
        if paper is None or paper.project_id != project_id:
            raise ValueError("选中的文献不属于当前项目")
        result = remove_paper(paper, delete_file=delete_file)
        totals["records"] += 1
        totals["relations"] += int(result["relations"])
        totals["claims"] += int(result["claims"])
        totals["plan_evidence"] += int(result["plan_evidence"])
        totals["files_deleted"] += int(bool(result.get("file_deleted")))
    log.info("批量移除文献 project=%s ids=%d totals=%s", project_id, len(paper_ids), totals)
    return totals


def _scrub_plan_references(project_id: str, paper_ids: set[str], claim_ids: set[str]) -> int:
    """删掉计划里指向已删文献的证据条目。返回被清理的条目数。

    留着指向不存在文献的"证据"比删掉更糟：报告会引用一个查不到的出处。
    """
    removed = 0
    for model_cls in (PlanVersion, PlanDraft):
        for record in repo.list_all(model_cls, project_id):
            kept = []
            for item in record.evidence or []:
                if not isinstance(item, dict):
                    kept.append(item)
                    continue
                if item.get("paper_id") in paper_ids or item.get("claim_id") in claim_ids:
                    removed += 1
                    continue
                kept.append(item)
            if len(kept) != len(record.evidence or []):
                record.evidence = kept
                repo.save(record)
    return removed


def _scrub_method_references(project_id: str, paper_ids: set[str]) -> int:
    touched = 0
    for method in repo.list_all(Method, project_id):
        kept = [pid for pid in method.source_paper_ids if pid not in paper_ids]
        if len(kept) != len(method.source_paper_ids):
            method.source_paper_ids = kept
            repo.save(method)
            touched += 1
    return touched


def deletion_impact(paper: Paper) -> dict[str, int]:
    """删除这篇文献会连带影响什么。界面先如实报数，再让用户确认。"""
    project_id = paper.project_id
    relations = repo.list_paper_relations(project_id, paper_id=paper.id, direction="both")
    claims = [claim for claim in repo.list_all(PaperClaim, project_id) if claim.paper_id == paper.id]
    records = repo.list_source_records(project_id, paper_id=paper.id)
    other_copies = [
        member
        for member in (entry_for(paper).members if entry_for(paper) else [])
        if member.id != paper.id
    ]
    return {
        "relations": len(relations),
        "claims": len(claims),
        "source_records": len(records),
        "other_copies": len(other_copies),
    }


def remove_paper(
    paper: Paper,
    *,
    delete_file: bool = False,
) -> dict[str, int | bool]:
    """从**本项目**移除一条文献记录，并清理它的派生数据。

    - 关系（两个方向）、AI 条目、外部查询记录、计划里的证据引用、方法来源
      一并清理，不留指向不存在文献的悬挂引用；
    - ``delete_file=True`` 且库里已没有别的项目引用同一份文件时，才删除 uploads 下的
      PDF 与按页正文；仍在别处被引用时**不删文件**（否则会把另一个项目读的文件删掉）。
    """
    project_id = paper.project_id
    claims = [claim for claim in repo.list_all(PaperClaim, project_id) if claim.paper_id == paper.id]
    claim_ids = {claim.id for claim in claims}
    paper_ids = {paper.id}

    relations = repo.list_paper_relations(project_id, paper_id=paper.id, direction="both")
    records = repo.list_source_records(project_id, paper_id=paper.id)
    for relation in relations:
        repo.delete(PaperRelation, relation.id)
    for claim in claims:
        repo.delete(PaperClaim, claim.id)
    for record in records:
        repo.delete(SourceRecord, record.id)

    scrubbed = _scrub_plan_references(project_id, paper_ids, claim_ids)
    methods = _scrub_method_references(project_id, paper_ids)

    file_deleted = False
    kept_elsewhere = False
    if delete_file:
        remaining = [
            other
            for other in repo.list_all(Paper)
            if other.id != paper.id and identity_key(other) == identity_key(paper)
        ]
        if remaining:
            kept_elsewhere = True
        else:
            file_deleted = _delete_paper_files(paper)

    repo.delete(Paper, paper.id)
    log.info(
        "移除文献 %s（项目 %s）：关系 %d、条目 %d、外部记录 %d、计划证据 %d",
        paper.id, project_id, len(relations), len(claims), len(records), scrubbed,
    )
    return {
        "relations": len(relations),
        "claims": len(claims),
        "plan_evidence": scrubbed,
        "methods": methods,
        "file_deleted": file_deleted,
        "file_kept_elsewhere": kept_elsewhere,
    }


def purge_entry(entry: LibraryEntry, *, delete_file: bool = True) -> dict[str, int | bool]:
    """把一篇文献从**所有项目**里删掉（文献库的彻底删除）。"""
    totals = {"records": 0, "relations": 0, "claims": 0, "plan_evidence": 0}
    for member in entry.members:
        result = remove_paper(member, delete_file=False)
        totals["records"] += 1
        totals["relations"] += int(result["relations"])
        totals["claims"] += int(result["claims"])
        totals["plan_evidence"] += int(result["plan_evidence"])

    file_deleted = False
    if delete_file:
        file_deleted = _delete_paper_files(entry.paper)
    totals["file_deleted"] = file_deleted
    return totals


def _delete_paper_files(paper: Paper) -> bool:
    """删除本地 PDF 与按页正文。文件不存在时也算处理完成（幂等）。"""
    removed = False
    pdf = paper_paths.resolve_local_path(paper.local_path)
    pages = paper_paths.pages_path(paper.local_path)
    for candidate in (pdf, pages):
        if candidate is None:
            continue
        try:
            if candidate.is_file():
                candidate.unlink()
                removed = True
        except OSError as exc:  # 文件被占用、只读等：如实记录，不假装成功
            log.warning("删除文件失败：%s（%s）", candidate, exc)
    return removed


def project_titles() -> dict[str, str]:
    """项目 id → 标题。库里列表要显示"这篇被哪些项目用着"。"""
    return {project.id: project.title for project in repo.list_all(Project)}


def total_entries() -> int:
    return len(entries())
