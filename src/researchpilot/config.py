"""配置与路径的统一出口。

规则：
1. 所有路径在这里计算，其他模块不得自己拼路径。
2. 密钥只从环境变量或 .env 读取，绝不写进代码或版本库。
3. 原始 SAC 数据目录只读引用，不做目录软链接。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# src/researchpilot/config.py → parents[2] 即仓库根
ROOT = Path(__file__).resolve().parents[2]
IS_WINDOWS = sys.platform == "win32"


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取，避免为一个小功能引入依赖。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")

def _load_streamlit_secrets() -> None:
    """云端部署用：把 Streamlit Secrets 读进环境变量。

    Streamlit Community Cloud 没有 .env，密钥填在控制台的 Secrets 里，
    而本项目的配置统一从环境变量读——这里做一次单向导入，
    使云端与本地行为一致。本地运行时 st.secrets 不存在，直接跳过。
    """
    keys = (
        "LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
        "OPENALEX_MAILTO", "CROSSREF_MAILTO",
        "RESEARCHPILOT_DB", "RESEARCHPILOT_DB_LABEL",
    )
    try:
        import streamlit as st

        secrets = st.secrets
    except Exception:  # noqa: BLE001 - 没有 Streamlit 运行时（脚本/测试）时正常退出
        return
    for key in keys:
        try:
            value = secrets.get(key)
        except Exception:  # noqa: BLE001
            continue
        if value and key not in os.environ:
            os.environ[key] = str(value)


_load_streamlit_secrets()



def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def dev_mode_enabled() -> bool:
    """是否开启开发者诊断模式（默认关闭）。

    这是**服务端授权开关**：由启动应用的人决定，普通访客无法通过 URL 打开诊断内容。
    需要按会话临时开启时，另配 RESEARCHPILOT_DEV_TOKEN，见 dev_token()。
    """
    return os.environ.get("RESEARCHPILOT_DEV_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def dev_token() -> str:
    """开发者诊断的会话令牌，未配置时返回空串。

    配置后可以用 `?dev=<令牌>` 只给单个会话开诊断（例如演示时临时查看环境信息）。
    没配令牌时 URL 参数一律无效——诊断不能仅凭公开 URL 打开。
    """
    return os.environ.get("RESEARCHPILOT_DEV_TOKEN", "").strip()


def _windows_wsl_root() -> Path:
    """Windows 侧访问 WSL 里的既有科研工作区。

    WSL 的 ext4 文件系统在 Windows 上通过 \\\\wsl.localhost\\<发行版>\\ 访问。
    发行版注册名不确定，所以逐个探测，全都不可用时返回最常见的一个，
    由调用方在界面上显示"不存在"，而不是抛异常。
    """
    for authority in ("wsl.localhost", "wsl$"):
        for distro in ("Ubuntu-22.04", "Ubuntu", "Ubuntu20.04"):
            candidate = Path(f"//{authority}/{distro}/home/wannnlee/seismic")
            if candidate.is_dir():
                return candidate
    return Path("//wsl.localhost/Ubuntu/home/wannnlee/seismic")


def _default_data_root() -> Path:
    """既有地震工作区的默认位置：Linux 直读，Windows 走 WSL 共享路径。"""
    return _windows_wsl_root() if IS_WINDOWS else Path("/home/wannnlee/seismic")


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    src: Path = ROOT / "src"
    outputs: Path = ROOT / "outputs"
    logs: Path = ROOT / "logs"
    demo_data: Path = ROOT / "demo_data"
    configs: Path = ROOT / "configs"
    projects: Path = ROOT / "outputs" / "projects"
    # 允许用 RESEARCHPILOT_DB 指到别的库：测试与迁移校验必须在副本上做，
    # 不能拿真实的 outputs/researchpilot.db 当试验场。不设该变量时行为与以前一致。
    db_path: Path = _env_path("RESEARCHPILOT_DB", ROOT / "outputs" / "researchpilot.db")
    uploads: Path = ROOT / "outputs" / "uploads"
    # 既有科研工作区，只读引用
    raw_sac_dir: Path = _env_path("RAW_SAC_DIR", _default_data_root() / "sac_data")
    legacy_project_dir: Path = _env_path(
        "LEGACY_PROJECT_DIR", _default_data_root() / "ambient_noise_test"
    )

    def ensure(self) -> None:
        for d in (self.outputs, self.logs, self.projects, self.uploads):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def platform_label(self) -> str:
        return "Windows" if IS_WINDOWS else "Linux/WSL"


# --------------------------------------------------------------------------
# 计算引擎（M2）：界面与数据库留在本环境，科研计算走独立解释器
# --------------------------------------------------------------------------

# 计算解释器：必须是自带 obspy / scipy / matplotlib 的那个环境。
# 不从界面传入，也不允许网页提交任意命令——只在这里配置一次。
#
# P0-C：默认值**按平台给**。以前不管在哪个平台都默认那个 POSIX 路径，于是 Windows
# 上的正式界面（8601）一启动就判定"计算环境不可用"，提示里还带着一个 Windows 上
# 根本不存在的 Linux 解释器路径。Windows 侧默认留空 = "未配置"，由 engine.env
# 给出"引擎在 WSL"的明确说明，而不是拿一个假路径去试。
DEFAULT_ENGINE_PYTHON: str = (
    "" if IS_WINDOWS else "/home/wannnlee/miniconda3/bin/python3"
)
ENGINE_PYTHON: str = os.environ.get(
    "RESEARCHPILOT_ENGINE_PYTHON", DEFAULT_ENGINE_PYTHON
).strip()

# 既有 Bensen 五段式脚本目录（只读，不写入）
ENGINE_CODE_DIR: Path = _env_path(
    "RESEARCHPILOT_ENGINE_CODE_DIR",
    Path("/home/wannnlee/seismic/ambient_noise_test/code"),
)

# 实验产物根目录：每次运行一个子目录，绝不落到原始数据或既有结果目录
RUNS_DIR: Path = ROOT / "outputs" / "runs"

# 允许作为输入的数据根目录（只读范围检查用）
ALLOWED_SAC_ROOT: Path = _env_path(
    "RESEARCHPILOT_SAC_ROOT", _default_data_root() / "sac_data"
)

# 数据库身份标签：临时验收库/测试库必须显式标识，避免被当成正式项目库
DB_LABEL: str = os.environ.get("RESEARCHPILOT_DB_LABEL", "").strip()


def db_identity() -> dict[str, object]:
    """当前进程连的是哪个库——启动时如实记录，用于排查"数据不见了"。

    要点：路径由 `__file__` 推导，**不随工作目录变化**；这里额外记录
    "启动时这个文件是否已存在"，新建空库时界面会明确提示，不再静默。
    """
    path = PATHS.db_path
    exists = path.is_file()
    return {
        "path": str(path),
        "existed_at_startup": exists,
        "size": path.stat().st_size if exists else 0,
        "label": DB_LABEL,
    }


# 各段默认超时（秒），可配置
STAGE_TIMEOUTS: dict[str, float] = {
    "qc": float(os.environ.get("RESEARCHPILOT_TIMEOUT_QC", "300") or 300),
    "preprocess": float(os.environ.get("RESEARCHPILOT_TIMEOUT_PREPROCESS", "900") or 900),
    "xcorr": float(os.environ.get("RESEARCHPILOT_TIMEOUT_XCORR", "600") or 600),
    "stack": float(os.environ.get("RESEARCHPILOT_TIMEOUT_STACK", "600") or 600),
}


@dataclass(frozen=True)
class LLMSettings:
    provider: str = os.environ.get("LLM_PROVIDER", "").strip()
    api_key: str = os.environ.get("LLM_API_KEY", "").strip()
    model: str = os.environ.get("LLM_MODEL", "").strip()
    base_url: str = os.environ.get("LLM_BASE_URL", "").strip()
    timeout: int = int(os.environ.get("LLM_TIMEOUT", "120") or 120)

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def describe(self) -> str:
        if self.available:
            return f"{self.provider or 'custom'} / {self.model}"
        missing = [
            name
            for name, value in (
                ("LLM_API_KEY", self.api_key),
                ("LLM_BASE_URL", self.base_url),
                ("LLM_MODEL", self.model),
            )
            if not value
        ]
        return "未配置（缺少 " + "、".join(missing) + "）"


@dataclass(frozen=True)
class BibliographySettings:
    openalex_mailto: str = os.environ.get("OPENALEX_MAILTO", "").strip()
    crossref_mailto: str = os.environ.get("CROSSREF_MAILTO", "").strip()
    timeout: int = int(os.environ.get("HTTP_TIMEOUT", "30") or 30)
    # 缓存与限速的默认值偏保守：这两个 API 都是免费公共服务，
    # 请求打太密会被限流，也会给别人添麻烦。
    cache_ttl_days: int = int(os.environ.get("HTTP_CACHE_TTL_DAYS", "7") or 7)
    min_interval: float = float(os.environ.get("HTTP_MIN_INTERVAL", "0.5") or 0.5)
    max_retries: int = int(os.environ.get("HTTP_MAX_RETRIES", "2") or 2)

    @property
    def contact(self) -> str:
        """写进 User-Agent 的联系邮箱；留空则不带（走公共池，更容易被限流）。"""
        return self.openalex_mailto or self.crossref_mailto

    @property
    def cache_dir(self) -> Path:
        return ROOT / "outputs" / "cache"

    def describe(self) -> str:
        who = self.contact or "未填联系邮箱（走公共池）"
        return (
            f"{who}｜超时 {self.timeout}s｜重试 {self.max_retries} 次｜"
            f"限速 {self.min_interval}s｜缓存 {self.cache_ttl_days} 天"
        )


PATHS = Paths()
LLM = LLMSettings()
BIBLIOGRAPHY = BibliographySettings()


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化日志：控制台 + logs/researchpilot.log。重复调用不会叠加 handler。"""
    PATHS.ensure()
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return logging.getLogger("researchpilot")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file_handler = logging.FileHandler(PATHS.logs / "researchpilot.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    return logging.getLogger("researchpilot")


APP_NAME = "ResearchPilot AI"
APP_VERSION = "1.2.0"
