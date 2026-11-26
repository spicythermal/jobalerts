"""Your own files, the examples shipped with jobalerts, and moving an older install over.

Everything personal lives in the data folder next to the config folder (gitignored, mounted in
Docker): profile.md, stories/, resumes/, companies.yaml and config.local.yaml. config.local.yaml
holds only the settings that differ from config/config.yaml and is merged on top of it.

A missing profile, watch list or story folder is filled from src/jobalerts/examples on first run.

Older installs kept their profile, watch list, stories and resumes in the repo. Before pulling the
version that stopped tracking them, tools/migrate_user_data.sh copies them into data/ and leaves
the old config.yaml as data/config.migrate.yaml (and taxes.yaml as data/taxes.migrate.yaml); the
next start turns those into config.local.yaml (only the values that differ from the new defaults,
without the old file locations, plus the household the old taxes.yaml held).
"""
from __future__ import annotations

import json
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


# Settings that a later version moved to a new name: old dotted name -> new one. config.local.yaml and
# the Settings page's saved values are carried over on load, so an update never loses them.
RENAMED: dict[str, str] = {}


def get_dotted(tree: dict, dotted: str):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def pop_dotted(tree: dict, dotted: str):
    """Remove a value from a nested dict (and dicts it leaves empty); returns it, or None."""
    parts = dotted.split(".")
    nodes = [tree]
    for part in parts[:-1]:
        nxt = nodes[-1].get(part) if isinstance(nodes[-1], dict) else None
        if not isinstance(nxt, dict):
            return None
        nodes.append(nxt)
    value = nodes[-1].pop(parts[-1], None) if isinstance(nodes[-1], dict) else None
    for node, part in zip(reversed(nodes[:-1]), reversed(parts[:-1])):
        if isinstance(node.get(part), dict) and not node[part]:
            del node[part]
    return value


def set_dotted(tree: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        tree = tree.setdefault(part, {})
    tree[parts[-1]] = value


def apply_renames(tree: dict) -> list[str]:
    """Move values under renamed settings to their new names (a value already under the new name wins)."""
    moved = []
    for old, new in RENAMED.items():
        value = pop_dotted(tree, old)
        if value is not None:
            if get_dotted(tree, new) is None:
                set_dotted(tree, new, value)
            moved.append(f"{old} → {new}")
    return moved


def validate_layered(model, base: dict, over: dict, where: str):
    """`over` on top of `base`, validated as `model`. A value of yours that this version can't use
    (renamed away, removed, or no longer valid) is dropped instead of stopping everything; the rest
    stays. Returns (validated config, problems in plain words, the part of `over` that was used).
    Errors in `base` still raise."""
    from pydantic import ValidationError
    over = json.loads(json.dumps(over))   # a copy; dropped keys mustn't touch the caller's tree
    problems = [f"renamed in this version: {m}" for m in apply_renames(over)]
    for _ in range(50):
        try:
            cfg = model.model_validate(deep_merge(base, over))
            break
        except ValidationError as e:
            dropped = False
            for err in e.errors():
                loc = [str(x) for x in err["loc"]]
                for n in range(len(loc), 0, -1):    # the longest part of the path that's yours
                    dotted = ".".join(loc[:n])
                    if get_dotted(over, dotted) is not None:
                        pop_dotted(over, dotted)
                        problems.append(f"{dotted} in {where} isn't valid in this version ({err['msg']}); ignored")
                        dropped = True
                        break
            if not dropped:
                raise
    else:
        raise ValueError(f"too many invalid settings in {where}")
    used = leaf_keys(cfg.model_dump())
    for key in sorted(leaf_keys(over)):
        if key not in used and not any(key.startswith(u + ".") for u in used):
            problems.append(f"{key} in {where} isn't used by this version; ignored")
            pop_dotted(over, key)
    return cfg, problems, over


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


def household_from_old_taxes(path: Path) -> dict | None:
    """The household an old config/taxes.yaml held (filing, 401(k) share, dependents' ages), in the
    Settings page's terms. The file is renamed once read."""
    if not path.is_file():
        return None
    old = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("household") or {}
    path.rename(path.with_suffix(".yaml.done"))
    if not old:
        return None
    ages = [a for a in old.get("dependents") or [] if isinstance(a, (int, float))]
    return {"filing": "joint" if old.get("filing") == "joint" else "single",
            "children": min(sum(a < 17 for a in ages), 4), "other_dependents": min(sum(a >= 17 for a in ages), 1),
            "k401_rate": float(old.get("k401_rate", 0.06))}


def migrate_old_config(config_path: Path, local_path: Path) -> bool:
    """data/config.migrate.yaml (an old config.yaml) -> data/config.local.yaml, once."""
    migrate = local_path.with_name("config.migrate.yaml")
    if local_path.exists() or not migrate.is_file():
        return False
    # Raw YAML on both sides: ${VAR} references stay references, so no secret is written out.
    old = yaml.safe_load(migrate.read_text(encoding="utf-8")) or {}
    new = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    diff = diff_config(old, new)
    if household := household_from_old_taxes(local_path.with_name("taxes.migrate.yaml")):
        diff["household"] = household
    local_path.write_text(LOCAL_HEADER + "\n" + yaml.safe_dump(diff, sort_keys=False, allow_unicode=True, width=110),
                          encoding="utf-8")
    migrate.rename(migrate.with_suffix(".yaml.done"))
    log.info("Moved your old settings into %s (%d sections)", local_path, len(diff))
    return True


def seed_user_files(cfg, watch_list: bool = True) -> list[str]:
    """Copy the shipped examples for whatever is missing (a new account: not the watch list, which is
    the install's). Returns what was created."""
    made = []
    targets = [(cfg.resolve(cfg.scoring.profile_path), EXAMPLES / "profile.md")]
    if watch_list:
        targets.append((cfg.resolve(cfg.companies_file), EXAMPLES / "companies.yaml"))
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
