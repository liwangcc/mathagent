from __future__ import annotations

from .capacity import SystemSnapshot


def inspect_system(*, cpu_sample_seconds: float = 0.2) -> SystemSnapshot:
    import psutil

    physical_cores = psutil.cpu_count(logical=False)
    logical_cores = psutil.cpu_count(logical=True)
    memory = psutil.virtual_memory()
    battery = psutil.sensors_battery()
    return SystemSnapshot(
        physical_cores=(int(physical_cores) if physical_cores is not None else None),
        logical_cores=(int(logical_cores) if logical_cores is not None else None),
        available_memory_bytes=int(memory.available),
        cpu_percent=float(psutil.cpu_percent(interval=cpu_sample_seconds)),
        on_battery=(None if battery is None else not bool(battery.power_plugged)),
    )
