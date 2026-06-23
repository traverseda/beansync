from __future__ import annotations

import dataclasses as _dc
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Union, get_args as _ga, get_origin as _go, get_type_hints as _gth

import yaml  # type: ignore[import-not-found]


@dataclass
class SecretRef:
    """A reference to a named secret stored in the keyring."""
    name: str

    def resolve(self) -> str:
        from beancountio.secrets import get_secret
        return get_secret(self.name)


MODEL = "openrouter/deepseek/deepseek-v4-flash"
VISION_MODEL = "openrouter/qwen/qwen2.5-vl-72b-instruct"
LEDGER = Path("main.bean")
MAX_RETRIES = 3
MAX_TOOL_ROUNDS = 3
MAX_SEARCHES = 3
CONFIG_FILE = Path("config.yaml")


# --- YAML loader/dumper (defined before plugins so register_plugin can use them) ---

class _Loader(yaml.SafeLoader):
    pass


class _Dumper(yaml.Dumper):
    pass


def _path_representer(dumper: yaml.Dumper, path: Path) -> yaml.ScalarNode:
    return dumper.represent_str(str(path))


def _secret_representer(dumper: yaml.Dumper, ref: SecretRef) -> yaml.ScalarNode:
    return dumper.represent_scalar("!secret", ref.name)


def _secret_constructor(loader: yaml.Loader, node: yaml.ScalarNode) -> SecretRef:
    return SecretRef(name=loader.construct_scalar(node))  # type: ignore[arg-type]


_Dumper.add_representer(Path, _path_representer)
_Dumper.add_representer(SecretRef, _secret_representer)
_Loader.add_constructor("!secret", _secret_constructor)


# --- Plugin registry ---

_PLUGIN_REGISTRY: dict[str, type] = {}


def register_plugin(cls: type) -> type:
    """Decorator: register a @dataclass as a bean-sync source plugin.

    Automatically wires YAML serialization (!ClassName tag) and adds the class
    to _PLUGIN_REGISTRY keyed by cls.plugin.

    Required ClassVars on the decorated class:
      plugin: str          — unique plugin identifier (e.g. "stagehand")
      _label: str          — human-readable name shown in the UI
      parse_mode: str      — "standard" | "image" (controls which LLM parser is used)

    The class must implement fetch(self, headed=False, since=None).
    """
    _PLUGIN_REGISTRY[cls.plugin] = cls  # type: ignore[attr-defined]
    fs = _dc.fields(cls)

    def _constructor(loader: yaml.Loader, node: yaml.MappingNode) -> object:
        hints = _gth(cls)
        d = loader.construct_mapping(node, deep=True)
        kwargs: dict = {}
        for f in fs:
            if f.name not in d:
                if f.default is not _dc.MISSING:
                    kwargs[f.name] = f.default
                elif f.default_factory is not _dc.MISSING:  # type: ignore[misc]
                    kwargs[f.name] = f.default_factory()  # type: ignore[misc]
                continue
            val = d[f.name]
            h = hints.get(f.name)
            if h is Path:
                val = Path(val) if val is not None else Path("")
            elif _go(h) is list and _ga(h) == (str,) and isinstance(val, str):
                val = [val]  # single string → list (e.g. EmailSource.sender)
            kwargs[f.name] = val
        return cls(**kwargs)

    def _representer(dumper: yaml.Dumper, s: object) -> yaml.MappingNode:
        d: dict = {}
        for f in fs:
            val = getattr(s, f.name)
            if isinstance(val, Path):
                val = str(val)
            d[f.name] = val
        return dumper.represent_mapping(f"!{cls.__name__}", d)

    _Loader.add_constructor(f"!{cls.__name__}", _constructor)
    _Dumper.add_representer(cls, _representer)
    return cls


# --- Source plugins ---

@register_plugin
@dataclass
class EmailSource:
    name: str
    source_dir: Path
    hint: str
    sender: list[str] = field(default_factory=list, metadata={"label": "Sender(s) — one per line"})
    nullable: bool = True
    enrichment: bool = False
    imap_host: str = field(default="imap.gmail.com", metadata={"label": "IMAP host"})
    imap_user: str = field(default="", metadata={"label": "IMAP user"})
    imap_password: Union[str, SecretRef] = field(default="", metadata={"label": "Password secret"})
    plugin: ClassVar[str] = "email"
    _label: ClassVar[str] = "Email"
    parse_mode: ClassVar[str] = "standard"

    def fetch(self, headed: bool = False, since=None) -> None:
        from beancountio.sync_email import fetch as _fetch
        _fetch(self, since=since)


@register_plugin
@dataclass
class InboxSource:
    name: str
    source_dir: Path
    hint: str
    nullable: bool = True
    enrichment: bool = True
    imap_host: str = field(default="imap.gmail.com", metadata={"label": "IMAP host"})
    imap_user: str = field(default="", metadata={"label": "IMAP user"})
    imap_password: Union[str, SecretRef] = field(default="", metadata={"label": "Password secret"})
    plugin: ClassVar[str] = "email-receipt"
    _label: ClassVar[str] = "Inbox (email-receipt)"
    parse_mode: ClassVar[str] = "integrated"

    def fetch(self, headed: bool = False, since=None) -> None:
        raise NotImplementedError("InboxSource uses integrated dispatch; call sync_email.ingest_receipt directly")


@register_plugin
@dataclass
class CUASource:
    name: str
    source_dir: Path
    hint: str
    account_name: str = field(default="", metadata={"label": "Account name"})
    days: int = field(default=30, metadata={"label": "Days"})
    nullable: bool = True
    enrichment: bool = False
    cua_user: Union[str, SecretRef] = field(default="", metadata={"label": "CUA user secret"})
    cua_password: Union[str, SecretRef] = field(default="", metadata={"label": "CUA password secret"})
    plugin: ClassVar[str] = "cua"
    _label: ClassVar[str] = "CUA (browser)"
    parse_mode: ClassVar[str] = "standard"

    def fetch(self, headed: bool = False, since=None) -> None:
        from beancountio.sync_cua import fetch_batch
        fetch_batch([self], headed=headed, since=since)


@register_plugin
@dataclass
class ImageSource:
    name: str
    source_dir: Path
    hint: str
    nullable: bool = True
    enrichment: bool = False
    plugin: ClassVar[str] = "image"
    _label: ClassVar[str] = "Image receipts"
    parse_mode: ClassVar[str] = "image"

    def fetch(self, headed: bool = False, since=None) -> None:
        pass  # images are dropped manually into source_dir


@register_plugin
@dataclass
class StagehandSource:
    name: str
    source_dir: Path
    hint: str
    script: str = field(default="", metadata={"label": "Script path"})
    secrets: dict[str, Union[str, SecretRef]] = field(
        default_factory=dict,
        metadata={"label": "Secrets (KEY=secret_name, one per line)"},
    )
    nullable: bool = True
    enrichment: bool = False
    plugin: ClassVar[str] = "stagehand"
    _label: ClassVar[str] = "Stagehand (browser)"
    parse_mode: ClassVar[str] = "standard"

    def fetch(self, headed: bool = False, since=None) -> None:
        from beancountio.sync_stagehand import fetch as _fetch
        _fetch(self, headed=headed, since=since)


AnySource = Union[EmailSource, InboxSource, CUASource, ImageSource, StagehandSource]


@dataclass
class Config:
    sources: list[AnySource] = field(default_factory=list)


def _config_representer(dumper: yaml.Dumper, c: Config) -> yaml.MappingNode:
    return dumper.represent_mapping("!Config", {"sources": c.sources})


def _config_constructor(loader: yaml.Loader, node: yaml.MappingNode) -> Config:
    d = loader.construct_mapping(node, deep=True)
    return Config(sources=d.get("sources", []))


_Loader.add_constructor("!Config", _config_constructor)
_Dumper.add_representer(Config, _config_representer)


def _source_from_dict(d: dict) -> AnySource:
    """Construct a source from a legacy plain-dict entry."""
    plugin = d.get("plugin", "email")
    cls = _PLUGIN_REGISTRY.get(plugin, EmailSource)
    hints = _gth(cls)
    kwargs: dict = {}
    for f in _dc.fields(cls):
        if f.name not in d:
            continue
        val = d[f.name]
        h = hints.get(f.name)
        if h is Path:
            val = Path(val) if val is not None else Path("")
        elif _go(h) is list and _ga(h) == (str,) and isinstance(val, str):
            val = [val]
        kwargs[f.name] = val
    kwargs.setdefault("name", d.get("name", "unknown"))
    kwargs.setdefault("source_dir", Path(d.get("source_dir", "sources/unknown")))
    kwargs.setdefault("hint", d.get("hint", ""))
    return cls(**kwargs)  # type: ignore[return-value]


def load_config() -> Config:
    raw = yaml.load(CONFIG_FILE.read_text(), Loader=_Loader)
    if isinstance(raw, Config):
        return raw
    # Legacy plain-dict format
    registered = tuple(_PLUGIN_REGISTRY.values())
    sources: list[AnySource] = []
    for s in raw.get("sources", []):
        if isinstance(s, registered):
            sources.append(s)  # type: ignore[arg-type]
        else:
            sources.append(_source_from_dict(s))
    return Config(sources=sources)


def save_config(config: Config) -> None:
    CONFIG_FILE.write_text(
        yaml.dump(config, Dumper=_Dumper, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )


def load_sources() -> list[AnySource]:
    return load_config().sources


def load_accounts() -> str:
    text = LEDGER.read_text()
    match = re.search(r'^\* Accounts$(.*?)(?=^\* |\Z)', text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else text


_PRIMARY_INCLUDES = Path("sources/_primary.bean")


def write_primary_includes(sources: list[AnySource]) -> None:
    """Rewrite sources/_primary.bean with include directives for all non-enrichment sources.

    Enrichment sources (enrichment=True) provide LLM context but are not part of the ledger.
    """
    _PRIMARY_INCLUDES.parent.mkdir(parents=True, exist_ok=True)
    lines = ["; Auto-generated by bean-sync — do not edit by hand.\n"]
    for source in sources:
        if not getattr(source, "enrichment", False):
            lines.append(f'include "{source.source_dir}/**/*.bean"\n')
    _PRIMARY_INCLUDES.write_text("".join(lines))
