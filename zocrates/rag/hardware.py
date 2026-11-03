"""
System hardware profiler and isolated Hugging Face embedding model validator.
"""

import glob
import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_PROCFS_DIR: str = os.environ.get("ZOCRATES_PROC_DIR", "/proc")
_SYSFS_DIR: str = os.environ.get("ZOCRATES_SYS_DIR", "/sys")


class DeviceLike(Protocol):
    """Protocol representing a PyTorch device-like object."""

    @property
    def type(self) -> str:
        """Device type string (e.g. 'cpu', 'cuda', 'mps')."""
        ...


@dataclass
class SystemHardwareInfo:
    """System hardware diagnostic information."""

    ram_gb: float
    gpu_vendor: str  # 'nvidia' | 'apple' | 'amd' | 'intel' | 'cpu'
    gpu_name: str
    vram_gb: float | None
    compute_capability: float | None

    def to_dict(self) -> dict[str, Any]:
        """Serialize hardware info to a JSON-serializable dict.

        Returns:
            Dict with keys ``ram_gb``, ``gpu_vendor``, ``gpu_name``,
            ``vram_gb``, and ``compute_capability``.
        """
        return {
            "ram_gb": round(self.ram_gb, 2),
            "gpu_vendor": self.gpu_vendor,
            "gpu_name": self.gpu_name,
            "vram_gb": round(self.vram_gb, 2) if self.vram_gb else None,
            "compute_capability": self.compute_capability,
        }


def get_system_ram_gb() -> float:
    """Return total system RAM in gigabytes.

    Attempts to read RAM via ``psutil`` first, then falls back to parsing
    ``/proc/meminfo``. Returns 8.0 as a safe default if both methods fail.

    Returns:
        Total RAM in GiB, rounded to two decimal places.
    """
    try:
        import psutil
        return psutil.virtual_memory().total / (1024**3)
    except Exception:
        pass

    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        kb = int(parts[1])
                        return kb / (1024**2)
    except Exception:
        pass

    return 8.0  # Safe default assumption if unreadable


_CACHED_HW_INFO: SystemHardwareInfo | None = None


def _detect_nvidia_torch() -> tuple[str, float, float] | None:
    """Attempt to detect NVIDIA GPU properties using PyTorch CUDA runtime.

    Returns:
        3-tuple of (gpu_name, vram_gb, compute_capability) if CUDA is available
        and at least one device is found, None otherwise.
    """
    try:
        import torch

        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            name = torch.cuda.get_device_name(0) or "NVIDIA GPU"
            props = torch.cuda.get_device_properties(0)
            vram_gb = props.total_memory / (1024**3)
            cc = float(f"{props.major}.{props.minor}")
            return name, vram_gb, cc
    except Exception:
        pass
    return None


def _detect_nvidia_procfs() -> str | None:
    """Read NVIDIA GPU model from /proc/driver/nvidia/gpus/*/information.

    Returns:
        Parsed model string (e.g. 'NVIDIA GeForce MX250') if found, None otherwise.
    """
    pattern = os.path.join(_PROCFS_DIR, "driver/nvidia/gpus/*/information")
    try:
        for info_path in sorted(glob.glob(pattern)):
            if os.path.isfile(info_path):
                with open(info_path, "r", errors="ignore") as f:
                    for line in f:
                        if line.startswith("Model:"):
                            parts = line.split(":", 1)
                            if len(parts) > 1 and parts[1].strip():
                                return parts[1].strip()
    except Exception:
        pass
    return None


def _detect_nvidia_pci() -> bool:
    """Check /sys/bus/pci/devices/* for an NVIDIA display controller (vendor 0x10de, class 0x03*).

    Returns:
        True if an NVIDIA display controller is found, False otherwise.
    """
    pattern = os.path.join(_SYSFS_DIR, "bus/pci/devices/*")
    try:
        for dev_dir in sorted(glob.glob(pattern)):
            vendor_file = os.path.join(dev_dir, "vendor")
            class_file = os.path.join(dev_dir, "class")
            if os.path.isfile(vendor_file) and os.path.isfile(class_file):
                try:
                    with open(vendor_file, "r", errors="ignore") as vf:
                        vendor = vf.read().strip().lower()
                    if vendor != "0x10de":
                        continue
                    with open(class_file, "r", errors="ignore") as cf:
                        pci_class = cf.read().strip().lower()
                    if pci_class.startswith("0x03"):
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _detect_amd_sysfs() -> tuple[str, float | None] | None:
    """Detect AMD GPU via Linux sysfs (/sys/class/drm/card* or /sys/bus/pci/drivers/amdgpu).

    Returns:
        2-tuple of (gpu_name, vram_gb) if an AMD GPU is found, None otherwise.
    """
    candidate_dirs: list[str] = []

    # 1. DRM card devices: /sys/class/drm/card[0-9]*/device
    drm_pattern = os.path.join(_SYSFS_DIR, "class/drm/card[0-9]*/device")
    try:
        candidate_dirs.extend(sorted(glob.glob(drm_pattern)))
    except Exception:
        pass

    # 2. AMDGPU PCI driver devices: /sys/bus/pci/drivers/amdgpu/[0-9a-fA-F]*
    amdgpu_pattern = os.path.join(_SYSFS_DIR, "bus/pci/drivers/amdgpu/[0-9a-fA-F]*")
    try:
        for p in sorted(glob.glob(amdgpu_pattern)):
            if os.path.isdir(p) and p not in candidate_dirs:
                candidate_dirs.append(p)
    except Exception:
        pass

    best_candidate: tuple[str, float | None] | None = None

    for dev_dir in candidate_dirs:
        vendor_file = os.path.join(dev_dir, "vendor")
        uevent_file = os.path.join(dev_dir, "uevent")
        is_amd = False

        if os.path.isfile(vendor_file):
            try:
                with open(vendor_file, "r", errors="ignore") as f:
                    if f.read().strip().lower() == "0x1002":
                        is_amd = True
            except Exception:
                pass

        if not is_amd and os.path.isfile(uevent_file):
            try:
                with open(uevent_file, "r", errors="ignore") as f:
                    if "DRIVER=amdgpu" in f.read():
                        is_amd = True
            except Exception:
                pass

        if not is_amd:
            continue

        # Product name
        product_name: str | None = None
        prod_file = os.path.join(dev_dir, "product_name")
        if os.path.isfile(prod_file):
            try:
                with open(prod_file, "r", errors="ignore") as f:
                    p_name = f.read().strip()
                    if p_name:
                        product_name = p_name
            except Exception:
                pass

        if not product_name:
            product_name = "AMD Radeon Graphics"

        # VRAM in GiB from mem_info_vram_total
        vram_gb: float | None = None
        vram_file = os.path.join(dev_dir, "mem_info_vram_total")
        if os.path.isfile(vram_file):
            try:
                with open(vram_file, "r", errors="ignore") as f:
                    vram_bytes = int(f.read().strip())
                    if vram_bytes > 0:
                        vram_gb = vram_bytes / (1024**3)
            except Exception:
                pass

        cand = (product_name, vram_gb)
        if best_candidate is None:
            best_candidate = cand
        elif vram_gb is not None and (best_candidate[1] is None or vram_gb > best_candidate[1]):
            best_candidate = cand

    return best_candidate


def _detect_intel_sysfs() -> tuple[str, float | None] | None:
    """Detect Intel GPU via Linux sysfs (/sys/class/drm/card* or /sys/bus/pci/devices/*).

    Returns:
        2-tuple of (gpu_name, vram_gb) if an Intel GPU is found, None otherwise.
    """
    candidate_dirs: list[str] = []

    # 1. DRM card devices
    drm_pattern = os.path.join(_SYSFS_DIR, "class/drm/card[0-9]*/device")
    try:
        candidate_dirs.extend(sorted(glob.glob(drm_pattern)))
    except Exception:
        pass

    # 2. PCI devices
    pci_pattern = os.path.join(_SYSFS_DIR, "bus/pci/devices/*")
    try:
        for p in sorted(glob.glob(pci_pattern)):
            if p not in candidate_dirs:
                candidate_dirs.append(p)
    except Exception:
        pass

    best_candidate: tuple[str, float | None] | None = None

    for dev_dir in candidate_dirs:
        vendor_file = os.path.join(dev_dir, "vendor")
        if not os.path.isfile(vendor_file):
            continue
        try:
            with open(vendor_file, "r", errors="ignore") as f:
                if f.read().strip().lower() != "0x8086":
                    continue
        except Exception:
            continue

        # Verify display controller class if checking PCI device
        class_file = os.path.join(dev_dir, "class")
        if os.path.isfile(class_file):
            try:
                with open(class_file, "r", errors="ignore") as f:
                    if not f.read().strip().lower().startswith("0x03"):
                        continue
            except Exception:
                continue

        # Product name if present
        product_name: str | None = None
        prod_file = os.path.join(dev_dir, "product_name")
        if os.path.isfile(prod_file):
            try:
                with open(prod_file, "r", errors="ignore") as f:
                    p_name = f.read().strip()
                    if p_name:
                        product_name = p_name
            except Exception:
                pass

        # VRAM in GiB from lmem_total_bytes or mem_info_vram_total (Arc / discrete)
        vram_gb: float | None = None
        for vram_filename in ("lmem_total_bytes", "mem_info_vram_total"):
            vram_file = os.path.join(dev_dir, vram_filename)
            if os.path.isfile(vram_file):
                try:
                    with open(vram_file, "r", errors="ignore") as f:
                        vram_bytes = int(f.read().strip())
                        if vram_bytes > 0:
                            vram_gb = vram_bytes / (1024**3)
                            break
                except Exception:
                    pass

        is_discrete = vram_gb is not None and vram_gb > 0
        if not product_name:
            product_name = "Intel Arc / Discrete Graphics" if is_discrete else "Intel Graphics"

        cand = (product_name, vram_gb)
        if best_candidate is None:
            best_candidate = cand
        elif is_discrete and (best_candidate[1] is None or (vram_gb and vram_gb > best_candidate[1])):
            best_candidate = cand

    return best_candidate


def get_system_hardware_info(*, force_refresh: bool = False) -> SystemHardwareInfo:
    """Probe system memory and GPU accelerator information.

    Caches result in memory to prevent repeated subprocess calls during server runtime.

    Checks, in priority order: Apple Silicon MPS, NVIDIA via ``nvidia-smi`` or
    PyTorch / procfs / sysfs fallback, AMD via ``rocm-smi`` or sysfs fallback,
    Intel via ``xpu-smi`` or sysfs fallback, then falls back to CPU-only.

    Returns:
        A ``SystemHardwareInfo`` dataclass populated with RAM, GPU vendor,
        GPU name, VRAM, and compute capability (``None`` for non-NVIDIA).
    """
    global _CACHED_HW_INFO
    if _CACHED_HW_INFO is not None and not force_refresh:
        return _CACHED_HW_INFO

    ram_gb = get_system_ram_gb()

    # 1. Apple Silicon (macOS MPS)
    if sys.platform == "darwin" and platform.machine() in ("arm64", "aarch64"):
        _CACHED_HW_INFO = SystemHardwareInfo(
            ram_gb=ram_gb,
            gpu_vendor="apple",
            gpu_name="Apple Silicon (MPS)",
            vram_gb=ram_gb,  # Unified Memory Architecture
            compute_capability=None,
        )
        return _CACHED_HW_INFO

    # 2. NVIDIA Detection
    # 2A: Try nvidia-smi CLI
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=compute_cap,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            line = output.splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            cc = float(parts[0])
            gpu_name = parts[1] if len(parts) > 1 else "NVIDIA GPU"
            vram_mb = float(parts[2]) if len(parts) > 2 else 0.0
            _CACHED_HW_INFO = SystemHardwareInfo(
                ram_gb=ram_gb,
                gpu_vendor="nvidia",
                gpu_name=gpu_name,
                vram_gb=vram_mb / 1024.0 if vram_mb else None,
                compute_capability=cc,
            )
            return _CACHED_HW_INFO
    except Exception:
        pass

    # 2B: Fallback via PyTorch / Procfs / Sysfs
    torch_info = _detect_nvidia_torch()
    if torch_info is not None:
        gpu_name, vram_gb, cc = torch_info
        _CACHED_HW_INFO = SystemHardwareInfo(
            ram_gb=ram_gb,
            gpu_vendor="nvidia",
            gpu_name=gpu_name,
            vram_gb=vram_gb,
            compute_capability=cc,
        )
        return _CACHED_HW_INFO

    procfs_model = _detect_nvidia_procfs()
    pci_found = _detect_nvidia_pci()
    if procfs_model or pci_found:
        _CACHED_HW_INFO = SystemHardwareInfo(
            ram_gb=ram_gb,
            gpu_vendor="nvidia",
            gpu_name=procfs_model or "NVIDIA GPU",
            vram_gb=None,
            compute_capability=None,
        )
        return _CACHED_HW_INFO

    # 3. AMD Detection
    # 3A: Try rocm-smi CLI
    try:
        output = subprocess.check_output(
            ["rocm-smi", "--showid"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            _CACHED_HW_INFO = SystemHardwareInfo(
                ram_gb=ram_gb,
                gpu_vendor="amd",
                gpu_name="AMD Radeon / Instinct (ROCm)",
                vram_gb=None,
                compute_capability=None,
            )
            return _CACHED_HW_INFO
    except Exception:
        pass

    # 3B: Fallback via Linux Sysfs
    amd_sysfs = _detect_amd_sysfs()
    if amd_sysfs is not None:
        amd_name, amd_vram = amd_sysfs
        _CACHED_HW_INFO = SystemHardwareInfo(
            ram_gb=ram_gb,
            gpu_vendor="amd",
            gpu_name=amd_name,
            vram_gb=amd_vram,
            compute_capability=None,
        )
        return _CACHED_HW_INFO

    # 4. Intel Detection
    # 4A: Try xpu-smi CLI
    try:
        output = subprocess.check_output(
            ["xpu-smi", "health"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            _CACHED_HW_INFO = SystemHardwareInfo(
                ram_gb=ram_gb,
                gpu_vendor="intel",
                gpu_name="Intel Arc / Data Center GPU (XPU)",
                vram_gb=None,
                compute_capability=None,
            )
            return _CACHED_HW_INFO
    except Exception:
        pass

    # 4B: Sysfs fallback for Intel discrete / Arc / iGPU
    intel_sysfs = _detect_intel_sysfs()
    if intel_sysfs is not None:
        intel_name, intel_vram = intel_sysfs
        _CACHED_HW_INFO = SystemHardwareInfo(
            ram_gb=ram_gb,
            gpu_vendor="intel",
            gpu_name=intel_name,
            vram_gb=intel_vram,
            compute_capability=None,
        )
        return _CACHED_HW_INFO

    # 5. CPU Fallback
    _CACHED_HW_INFO = SystemHardwareInfo(
        ram_gb=ram_gb,
        gpu_vendor="cpu",
        gpu_name=platform.processor() or "CPU",
        vram_gb=None,
        compute_capability=None,
    )
    return _CACHED_HW_INFO


def is_high_performance_gpu() -> bool:
    """Check if the system possesses a high-performance GPU suitable for deep learning formula VLM.

    Returns True if:
      - NVIDIA GPU with >= 6.0 GB VRAM
      - Apple Silicon (MPS) with >= 16.0 GB unified RAM
      - Other verified GPU with >= 6.0 GB VRAM

    Returns:
        True if high-performance GPU is available, False otherwise.
    """
    info = get_system_hardware_info()
    if info.gpu_vendor == "apple":
        return info.ram_gb >= 16.0
    if info.gpu_vendor == "nvidia":
        return info.vram_gb is not None and info.vram_gb >= 6.0
    # For other vendors (AMD/Intel), only qualify as high-performance if PyTorch has functional
    # accelerator runtime support (e.g. torch ROCm / XPU) and adequate VRAM (>= 6GB).
    if info.vram_gb is not None and info.vram_gb >= 6.0:
        try:
            import torch
            if torch.cuda.is_available() or (hasattr(torch, "xpu") and torch.xpu.is_available()):
                return True
        except Exception:
            pass
    return False


def get_docling_hardware_assessment() -> dict[str, Any]:
    """Assess system hardware capacity and recommend optimal local Docling configuration.

    Returns:
        Dict with keys ``recommended_mode`` ("fast" | "full"), ``is_gpu_accelerated`` (bool),
        ``vram_gb`` (float | None), and ``reason`` (str).
    """
    info = get_system_hardware_info()
    high_perf = is_high_performance_gpu()

    if high_perf:
        vram_str = f"{info.vram_gb:.1f} GB VRAM" if info.vram_gb else f"{info.ram_gb:.1f} GB Unified Memory"
        return {
            "recommended_mode": "full",
            "is_gpu_accelerated": True,
            "vram_gb": info.vram_gb,
            "reason": f"High-performance accelerator detected ({info.gpu_name}, {vram_str}). Running in Full Mode (GPU layout, tables, and formula LaTeX VLM enabled).",
        }

    if info.gpu_vendor == "nvidia":
        if info.vram_gb is not None:
            reason = (
                f"GPU VRAM ({info.vram_gb:.1f} GB) is below the 6.0 GB threshold for heavy formula VLM. "
                f"Running in Fast Mode (layout & tables on CPU). For full LaTeX math OCR on this system, use Datalab Chandra (Cloud)."
            )
        else:
            reason = (
                f"Detected {info.gpu_name} with undetermined VRAM. "
                f"Running in Fast Mode (layout & tables on CPU). For full LaTeX math OCR on this system, use Datalab Chandra (Cloud)."
            )
        return {
            "recommended_mode": "fast",
            "is_gpu_accelerated": False,
            "vram_gb": info.vram_gb,
            "reason": reason,
        }

    if info.gpu_vendor == "apple":
        return {
            "recommended_mode": "fast",
            "is_gpu_accelerated": False,
            "vram_gb": None,
            "reason": f"Unified Memory ({info.ram_gb:.1f} GB) is below the 16.0 GB threshold. Running in Fast Mode (layout & tables on CPU).",
        }

    if info.gpu_vendor in ("amd", "intel"):
        vram_clause = f" ({info.vram_gb:.1f} GB VRAM)" if info.vram_gb is not None else ""
        return {
            "recommended_mode": "fast",
            "is_gpu_accelerated": False,
            "vram_gb": info.vram_gb,
            "reason": f"Detected {info.gpu_name}{vram_clause}. Running in Fast Mode (layout & tables on CPU). For full LaTeX math OCR on this system, use Datalab Chandra (Cloud).",
        }

    return {
        "recommended_mode": "fast",
        "is_gpu_accelerated": False,
        "vram_gb": None,
        "reason": f"Running on {info.gpu_name} (CPU). Running in Fast Mode (layout & tables on CPU). For full LaTeX math OCR on this system, use Datalab Chandra (Cloud).",
    }


def validate_model_compatibility(model_name: str, timeout_seconds: int = 100) -> tuple[bool, str]:
    """
    Run an isolated subprocess test to load and encode a test string with the model.

    Spawns a fresh Python process to avoid polluting the caller's GPU memory
    state and to enforce a wall-clock timeout.

    Args:
        model_name: Hugging Face model identifier (e.g.
            ``"jinaai/jina-embeddings-v5-text-nano"``).
        timeout_seconds: Maximum seconds to wait for the subprocess.

    Returns:
        A 2-tuple of ``(success, error_message)``. On success,
        ``error_message`` is ``"Success"``.
    """
    cmd = [
        sys.executable,
        "-c",
        f"""
import sys
try:
    from transformers import AutoModel
    import torch
    model = AutoModel.from_pretrained("{model_name}", trust_remote_code=True)
    if hasattr(model, "encode"):
        try:
            emb = model.encode(["test"], task="retrieval")
        except TypeError:
            emb = model.encode(["test"])
    else:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained("{model_name}", trust_remote_code=True)
        inp = tok("test", return_tensors="pt")
        with torch.no_grad():
            out = model(**inp)
    sys.exit(0)
except Exception as e:
    print(str(e))
    sys.exit(1)
""",
    ]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if res.returncode == 0:
            return True, "Success"
        err = res.stdout.strip() or res.stderr.strip() or "Unknown loading failure"
        return False, err
    except subprocess.TimeoutExpired:
        return False, f"Model loading timed out after {timeout_seconds}s"
    except Exception as exc:
        return False, str(exc)


def _is_cuda_functional() -> bool:
    """Check in an isolated subprocess whether CUDA hardware execution works on this system.

    If the first CUDA test fails, automatically invokes ``setup_torch.main()``
    to install the matching PyTorch CUDA build, then re-tests.

    Returns:
        ``True`` if a CUDA tensor operation completes successfully,
        ``False`` otherwise.
    """
    try:
        import subprocess, sys
        res = subprocess.run(
            [sys.executable, "-c", "import torch; assert torch.cuda.is_available(); (torch.zeros(1).cuda() + 1).item()"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return True

        # CUDA test failed. Attempt automatic setup/repair for the user's specific GPU
        try:
            from zocrates.rag.setup_torch import main as setup_torch_main
            logger.info("PyTorch CUDA execution check failed. Invoking setup_torch to configure matching CUDA build...")
            setup_torch_main()

            res = subprocess.run(
                [sys.executable, "-c", "import torch; assert torch.cuda.is_available(); (torch.zeros(1).cuda() + 1).item()"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return res.returncode == 0
        except Exception as repair_exc:
            logger.warning("Auto PyTorch CUDA setup failed: %s", repair_exc)
            return False
    except Exception:
        return False


def _detect_best_device() -> DeviceLike:
    """Return the optimal torch device to use for local embedding inference.

    Opportunistically probes CUDA and Apple Silicon MPS with a lightweight
    tensor sanity probe. Gracefully falls back to CPU if probes fail or no
    GPU accelerator is available.

    Returns:
        A ``torch.device`` instance for ``"cuda"``, ``"mps"``, or ``"cpu"``.
    """
    import torch

    # 1. Probe NVIDIA CUDA
    try:
        cuda_avail = torch.cuda.is_available()
    except Exception as exc:
        logger.warning("torch.cuda.is_available() check failed: %s", exc)
        cuda_avail = False

    if cuda_avail:
        try:
            probe = torch.zeros(1, device="cuda")
            del probe
            logger.info("Opportunistic GPU acceleration enabled for embeddings (cuda).")
            return torch.device("cuda")
        except Exception as exc:
            logger.warning(
                "CUDA detected but tensor sanity probe failed (%s: %s). Falling back.",
                type(exc).__name__,
                exc,
            )

    # 2. Probe Apple Silicon MPS
    try:
        mps_avail = hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    except Exception as exc:
        logger.warning("torch.backends.mps check failed: %s", exc)
        mps_avail = False

    if mps_avail:
        try:
            probe = torch.zeros(1, device="mps")
            del probe
            logger.info("Opportunistic GPU acceleration enabled for embeddings (mps).")
            return torch.device("mps")
        except Exception as exc:
            logger.warning(
                "MPS detected but tensor sanity probe failed (%s: %s). Falling back to CPU.",
                type(exc).__name__,
                exc,
            )

    # 3. Default fallback: CPU
    logger.info("Using CPU hardware execution for local embeddings.")
    return torch.device("cpu")
