"""Your own files, the examples shipped with jobalerts, and moving an older install over.

Everything personal lives in the data folder next to the config folder (gitignored, mounted in
Docker): profile.md, stories/, resumes/, companies.yaml and config.local.yaml. config.local.yaml
holds only the settings that differ from config/config.yaml and is merged on top of it.

A missing profile, watch list or story folder is filled from src/jobalerts/examples on first run.

Older installs kept their profile, watch list, stories and resumes in the repo. Before pulling the
version that stopped tracking them, tools/migrate_user_data.sh copies them into data/ and leaves
the old config.yaml as data/config.migrate.yaml; the next start turns that into config.local.yaml
(only the values that differ from the new defaults, without the old file locations).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

EXAMPLES = Path(__file__).parent / "examples"
# Old file locations that must not carry over from a migrated config.yaml.
PATH_KEYS = {("companies_file",), ("database",), ("scoring", "profile_path"), ("scoring", "resume_dir"),
             ("scoring", "stories_dir"), ("pay", "col_data"), ("pay", "tax_rules"), ("pay", "tax_data")}
LOCAL_HEADER = ("# Your settings: only what differs from config/config.yaml, merged on top of it.\n"
                "# Not tracked by git. Secrets stay in .env or on the Settings page, never here.\n")


def local_config_path(config_path: Path) -> Path:
    if os.environ.get("JOBALERTS_LOCAL_CONFIG"):
        return Path(os.environ["JOBALERTS_LOCAL_CONFIG"])
    return config_path.parent.parent / "data" / "config.local.yaml"


def deep_merge(base: dict, over: dict) -> dict:
    """`over` on top of `base`: dicts merge key by key; lists and values replace."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def leaf_keys(tree: dict, prefix: str = "") -> set[str]:
    """Dotted names of the values in a nested dict ("pay.current_home.payment")."""
    keys = set()
    for key, value in tree.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            keys |= leaf_keys(value, name + ".")
        else:
            keys.add(name)
    return keys


def diff_config(old: dict, new: dict, path: tuple = ()) -> dict:
    """The values in `old` that differ from `new`, leaving out old file locations."""
    out = {}
    for key, value in old.items():
        here = path + (key,)
        if here in PATH_KEYS:
            continue
        if isinstance(value, dict) and isinstance(new.get(key), dict):
            sub = diff_config(value, new[key], here)
            if sub:
                out[key] = sub
        elif key not in new or new[key] != value:
            out[key] = value
    return out


def migrate_old_config(config_path: Path, local_path: Path) -> bool:
    """data/config.migrate.yaml (an old config.yaml) -> data/config.local.yaml, once."""
    migrate = local_path.with_name("config.migrate.yaml")
    if local_path.exists() or not migrate.is_file():
        return False
    # Raw YAML on both sides: ${VAR} references stay references, so no secret is written out.
    old = yaml.safe_load(migrate.read_text(encoding="utf-8")) or {}
    new = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    diff = diff_config(old, new)
    local_path.write_text(LOCAL_HEADER + "\n" + yaml.safe_dump(diff, sort_keys=False, allow_unicode=True, width=110),
                          encoding="utf-8")
    migrate.rename(migrate.with_suffix(".yaml.done"))
    log.info("Moved your old settings into %s (%d sections)", local_path, len(diff))
    return True


def seed_user_files(cfg) -> list[str]:
    """Copy the shipped examples for whatever is missing. Returns what was created."""
    made = []
    targets = [(cfg.resolve(cfg.scoring.profile_path), EXAMPLES / "profile.md"),
               (cfg.resolve(cfg.companies_file), EXAMPLES / "companies.yaml")]
    for target, example in targets:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example, target)
            made.append(str(target))
    stories = cfg.resolve(cfg.scoring.stories_dir)
    if not stories.exists():
        stories.mkdir(parents=True)
        for example in sorted((EXAMPLES / "stories").glob("*.md")):
            shutil.copyfile(example, stories / example.name)
        made.append(str(stories))
    cfg.resolve(cfg.scoring.resume_dir).mkdir(parents=True, exist_ok=True)
    for path in made:
        log.info("Created %s from the shipped example; edit it on the web UI or in place", path)
    return made
