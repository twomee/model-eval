"""Business logic for the Models screen. No HTTP here: routes translate, this decides.

The config is loaded on EVERY call instead of once at startup. Reading a small YAML
file takes well under a millisecond, and it means edits to config.yaml show up on
the next refresh, with no cache to invalidate and no restart.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...config import Config, ModelSpec, load_config, load_tasks
from ...doctor import judge_vendors, run_doctor, vendor_of
from ..schemas import CheckOut, DoctorOut, ModelOut, ModelsOut


class UnknownModelsError(ValueError):
    def __init__(self, names: set[str]):
        super().__init__(f"unknown model/judge names: {sorted(names)}")
        self.names = names


@dataclass(frozen=True)
class ModelsService:
    config_path: Path
    tasks_dir: Path

    def _config(self) -> Config:
        return load_config(self.config_path)

    def list_models(self) -> ModelsOut:
        cfg = self._config()
        return ModelsOut(config_path=str(self.config_path),
                         models=[_to_out(m, "model") for m in cfg.models] + [_to_out(j, "judge") for j in cfg.judges])

    async def doctor(self, names: list[str]) -> DoctorOut:
        cfg = self._config()
        wanted = set(names)
        if unknown := wanted - {s.name for s in (*cfg.models, *cfg.judges)}:
            raise UnknownModelsError(unknown)
        models = [m for m in cfg.models if not wanted or m.name in wanted]
        judges = [j for j in cfg.judges if not wanted or j.name in wanted]
        cfg_subset = Config(run=cfg.run, models=models, judges=judges, category_weights=cfg.category_weights)
        task_ids = [t.id for t in load_tasks(self.tasks_dir)]
        checks = await run_doctor(cfg_subset, models, task_ids)

        warnings = []
        # The vendor warning only makes sense when the whole judge panel was checked.
        vendors = sorted(judge_vendors(cfg_subset, checks))
        if judges == cfg.judges and len(vendors) < 2:
            warnings.append(f"Working judges cover {len(vendors)} vendor(s). Rubric scores will carry "
                            "that family's self-preference bias; add a judge from another vendor.")
        return DoctorOut(checks=[CheckOut(**c.__dict__) for c in checks], all_ok=all(c.ok for c in checks),
                         judge_vendors=vendors, warnings=warnings)


def _to_out(spec: ModelSpec, role: str) -> ModelOut:
    return ModelOut(name=spec.name, role=role, provider=spec.provider, vendor=vendor_of(spec), model_id=spec.model_id)
