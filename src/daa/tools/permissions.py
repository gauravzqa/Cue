"""TCC probes that never prompt and never raise.

macOS attaches privacy grants to the *host binary*, not to this package. Running
under `.venv/bin/python`, under `/usr/bin/python3`, under a packaged .app, or
under pytest are four different subjects to TCC, and each gets its own grant.
That is why this module reports what the CURRENT process can do rather than what
the product "supports", and why every probe is non-prompting: a permission
dialog raised from inside `resolve()` would block the voice loop on a modal the
user cannot see coming.

A missing grant must degrade exactly one tool. Nothing here raises; failures are
reported as False plus a remedy sentence the assistant can read aloud.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

ACCESSIBILITY = "accessibility"
AUTOMATION = "automation"
SCREEN_RECORDING = "screen_recording"

_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_"

REMEDY: Mapping[str, str] = {
    ACCESSIBILITY: (
        "Open System Settings, Privacy and Security, Accessibility, and turn on the app "
        "you launched me from."
    ),
    AUTOMATION: (
        "The first time I control another app macOS will ask for permission. "
        "Say yes once, or enable it under Privacy and Security, Automation."
    ),
    SCREEN_RECORDING: (
        "Open System Settings, Privacy and Security, Screen Recording, and turn on the app "
        "you launched me from. Window titles stay hidden until you do."
    ),
}

SETTINGS_URL: Mapping[str, str] = {
    ACCESSIBILITY: _PANE + "Accessibility",
    AUTOMATION: _PANE + "Automation",
    SCREEN_RECORDING: _PANE + "ScreenCapture",
}


@dataclass(frozen=True, slots=True)
class PermissionState:
    name: str
    granted: bool
    # Tri-state truth behind the bool: TCC distinguishes "denied" from "never
    # asked", and the guidance for those two is completely different.
    status: str          # "granted" | "denied" | "not_determined" | "unknown"
    remedy: str
    detail: str = ""
    settings_url: str = ""


def _host_binary() -> str:
    return sys.executable or "python"


# ---------------------------------------------------------------------------
# Accessibility
# ---------------------------------------------------------------------------


def _accessibility() -> PermissionState:
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
    except Exception as exc:  # noqa: BLE001 - non-mac or missing pyobjc
        return PermissionState(
            ACCESSIBILITY, False, "unknown", REMEDY[ACCESSIBILITY], f"pyobjc unavailable: {exc}",
            SETTINGS_URL[ACCESSIBILITY],
        )
    try:
        # AXIsProcessTrusted is the read-only form. The ...WithOptions variant
        # with kAXTrustedCheckOptionPrompt would raise a dialog, which is
        # exactly what a "check" must never do.
        trusted = bool(AXIsProcessTrusted())
    except Exception as exc:  # noqa: BLE001
        return PermissionState(
            ACCESSIBILITY, False, "unknown", REMEDY[ACCESSIBILITY], str(exc),
            SETTINGS_URL[ACCESSIBILITY],
        )
    return PermissionState(
        ACCESSIBILITY,
        trusted,
        "granted" if trusted else "denied",
        "" if trusted else REMEDY[ACCESSIBILITY],
        "" if trusted else f"{_host_binary()} is not a trusted accessibility client",
        SETTINGS_URL[ACCESSIBILITY],
    )


# ---------------------------------------------------------------------------
# Screen Recording -- gates window TITLES, not window geometry
# ---------------------------------------------------------------------------


def _screen_recording() -> PermissionState:
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return PermissionState(
            SCREEN_RECORDING, False, "unknown", REMEDY[SCREEN_RECORDING],
            f"pyobjc unavailable: {exc}", SETTINGS_URL[SCREEN_RECORDING],
        )
    try:
        # Preflight, not Request: Request() prompts.
        granted = bool(CGPreflightScreenCaptureAccess())
    except Exception as exc:  # noqa: BLE001
        return PermissionState(
            SCREEN_RECORDING, False, "unknown", REMEDY[SCREEN_RECORDING], str(exc),
            SETTINGS_URL[SCREEN_RECORDING],
        )
    return PermissionState(
        SCREEN_RECORDING,
        granted,
        "granted" if granted else "denied",
        "" if granted else REMEDY[SCREEN_RECORDING],
        "" if granted else "window titles will be omitted from list_windows",
        SETTINGS_URL[SCREEN_RECORDING],
    )


# ---------------------------------------------------------------------------
# Automation (AppleEvents)
# ---------------------------------------------------------------------------

_AE_NOT_PERMITTED = -1743          # errAEEventNotPermitted: user said no
_AE_WOULD_REQUIRE_CONSENT = -1744  # errAEEventWouldRequireUserConsent: never asked
_PROC_NOT_FOUND = -600             # target app is not running; says nothing about TCC


class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


@lru_cache(maxsize=1)
def _appservices() -> ctypes.CDLL | None:
    try:
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        lib = ctypes.CDLL(path)
        lib.AECreateDesc.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)
        ]
        lib.AECreateDesc.restype = ctypes.c_int32
        lib.AEDeterminePermissionToAutomateTarget.argtypes = [
            ctypes.POINTER(_AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool
        ]
        lib.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
        lib.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
        lib.AEDisposeDesc.restype = ctypes.c_int32
        return lib
    except Exception:  # noqa: BLE001
        return None


def automation_status(bundle_id: str = "com.apple.finder") -> PermissionState:
    """Ask TCC about one target WITHOUT prompting.

    AEDeterminePermissionToAutomateTarget with askUserIfNeeded=False is the only
    non-prompting answer macOS offers, and it is per-target: being allowed to
    drive Finder says nothing about Music. Finder is the default probe because
    it is always installed and is what files.move_to_trash falls back to.
    """
    lib = _appservices()
    if lib is None:
        return PermissionState(
            AUTOMATION, False, "unknown", REMEDY[AUTOMATION],
            "ApplicationServices could not be loaded", SETTINGS_URL[AUTOMATION],
        )
    desc = _AEDesc()
    raw = bundle_id.encode("utf-8")
    try:
        if lib.AECreateDesc(_fourcc("bund"), raw, len(raw), ctypes.byref(desc)) != 0:
            return PermissionState(
                AUTOMATION, False, "unknown", REMEDY[AUTOMATION],
                f"could not address {bundle_id}", SETTINGS_URL[AUTOMATION],
            )
        try:
            status = int(
                lib.AEDeterminePermissionToAutomateTarget(
                    ctypes.byref(desc), _fourcc("****"), _fourcc("****"), False
                )
            )
        finally:
            lib.AEDisposeDesc(ctypes.byref(desc))
    except Exception as exc:  # noqa: BLE001
        return PermissionState(
            AUTOMATION, False, "unknown", REMEDY[AUTOMATION], str(exc), SETTINGS_URL[AUTOMATION]
        )

    if status == 0:
        return PermissionState(AUTOMATION, True, "granted", "", "", SETTINGS_URL[AUTOMATION])
    if status == _AE_NOT_PERMITTED:
        return PermissionState(
            AUTOMATION, False, "denied",
            f"Automation for {bundle_id} was turned off. " + REMEDY[AUTOMATION],
            f"errAEEventNotPermitted for {bundle_id}", SETTINGS_URL[AUTOMATION],
        )
    if status == _AE_WOULD_REQUIRE_CONSENT:
        return PermissionState(
            AUTOMATION, False, "not_determined", REMEDY[AUTOMATION],
            f"macOS has not yet asked about {bundle_id}", SETTINGS_URL[AUTOMATION],
        )
    if status == _PROC_NOT_FOUND:
        return PermissionState(
            AUTOMATION, False, "unknown", REMEDY[AUTOMATION],
            f"{bundle_id} is not running, so macOS cannot answer yet",
            SETTINGS_URL[AUTOMATION],
        )
    return PermissionState(
        AUTOMATION, False, "unknown", REMEDY[AUTOMATION], f"OSStatus {status}",
        SETTINGS_URL[AUTOMATION],
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def permission_report() -> dict[str, PermissionState]:
    """Every probe, tri-state, with spoken remedies. Never raises."""
    if sys.platform != "darwin":
        return {
            name: PermissionState(
                name, False, "unknown", REMEDY[name], "not running on macOS",
                SETTINGS_URL[name],
            )
            for name in (ACCESSIBILITY, AUTOMATION, SCREEN_RECORDING)
        }
    return {
        ACCESSIBILITY: _accessibility(),
        AUTOMATION: automation_status(),
        SCREEN_RECORDING: _screen_recording(),
    }


def check_permissions() -> dict[str, bool]:
    """The flat answer other subsystems gate on."""
    return {name: state.granted for name, state in permission_report().items()}


def guidance() -> list[str]:
    """Spoken, actionable sentences for whatever is missing. Empty when fine."""
    out: list[str] = []
    for state in permission_report().values():
        if not state.granted and state.remedy:
            out.append(state.remedy)
    return out


def require(capability: str) -> PermissionState:
    """Look up one capability. Callers branch on `.granted` and degrade."""
    report = permission_report()
    if capability not in report:
        return PermissionState(capability, False, "unknown", "", f"unknown capability {capability}")
    return report[capability]


def describe() -> str:
    """One block of text for `daa doctor`, including which binary TCC sees."""
    lines = [f"TCC subject: {_host_binary()}", f"pid {os.getpid()}"]
    for name, state in permission_report().items():
        lines.append(f"  {name:<16} {state.status}")
        if state.remedy:
            lines.append(f"      -> {state.remedy}")
    return "\n".join(lines)
