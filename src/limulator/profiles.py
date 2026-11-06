"""Pinned Limulator guest profiles and their Apple firmware source metadata.

Product identities and native display sizes follow the pinned vphone-cli model
mapping (vphone-cli 2.14.2, revision 83197152137ddd2aad70eb121d6b88a9afbee36)
and Apple technical specifications for [iPhone 16](https://support.apple.com/de-de/121029)
and [iPad mini (A17 Pro)](https://support.apple.com/it-it/121456). Restore URLs,
sizes, and SHA-256 values are pinned from Apple firmware metadata and CDN responses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypedDict
from urllib.parse import urlsplit

ProfileCategory = Literal["phone", "tablet", "foldable"]
MetadataStatus = Literal["verified", "unverified", "unavailable"]


@dataclass(frozen=True, slots=True)
class FirmwareDescriptor:
    """A firmware artifact and the verification state of its source metadata.

    ``verified`` means the filename, Apple URL, size, and digest are pinned in
    the catalog. It does not mean the artifact has been downloaded locally.
    """

    filename: str | None
    url: str | None
    sha256: str | None
    size: int | None
    metadata_status: MetadataStatus

    def __post_init__(self) -> None:
        values = (self.filename, self.url, self.sha256, self.size)
        if self.metadata_status not in ("verified", "unverified", "unavailable"):
            raise ValueError(f"unsupported firmware metadata status: {self.metadata_status}")
        if self.metadata_status == "unavailable":
            if any(value is not None for value in values):
                raise ValueError("unavailable firmware metadata must not contain artifact fields")
            return

        if self.sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("firmware SHA-256 must be 64 lowercase hexadecimal characters")
        if self.size is not None and (type(self.size) is not int or self.size <= 0):
            raise ValueError("firmware size must be a positive integer")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("firmware URL must be HTTPS and must not contain credentials")

        if self.metadata_status == "verified" and any(value is None for value in values):
            raise ValueError("verified firmware metadata requires filename, URL, SHA-256 and size")

    @classmethod
    def unavailable(cls) -> FirmwareDescriptor:
        """Represent an artifact whose source metadata is not available yet."""
        return cls(None, None, None, None, "unavailable")

    @property
    def metadata_ready(self) -> bool:
        return self.metadata_status == "verified"


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    id: str
    label: str
    category: ProfileCategory
    model: str
    os_version: str
    width: int
    height: int
    scale: int
    pixels_per_inch: int
    upstream_linux_tested: bool
    main_firmware: FirmwareDescriptor
    cloud_os_firmware: FirmwareDescriptor

    def __post_init__(self) -> None:
        for name in ("width", "height", "scale", "pixels_per_inch"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"profile {name} must be a positive integer")
        if self.width % self.scale or self.height % self.scale:
            raise ValueError("profile display dimensions must resolve to integer points")

    @property
    def points(self) -> tuple[int, int]:
        """Display size in UIKit points, derived from native pixels and scale."""
        return self.width // self.scale, self.height // self.scale

    @property
    def firmware_metadata_ready(self) -> bool:
        """Whether both pinned firmware artifacts have complete source metadata."""
        return self.main_firmware.metadata_ready and self.cloud_os_firmware.metadata_ready


class PublicProfileMetadata(TypedDict):
    id: str
    label: str
    category: ProfileCategory
    model: str
    os_version: str
    width: int
    height: int
    scale: int
    pixels_per_inch: int
    upstream_linux_tested: bool
    firmware_metadata_ready: bool


def _firmware(filename: str, url: str, sha256: str, size: int) -> FirmwareDescriptor:
    return FirmwareDescriptor(filename, url, sha256, size, "verified")


_CLOUD_OS = _firmware(
    "cloudOS-23E5207q.ipsw",
    "https://updates.cdn-apple.com/private-cloud-compute/"
    "c0ecdb4b310cf5239ab2b248dd3098eec297dc5aa3bbe6ada27273262b0b8b64",
    "c0ecdb4b310cf5239ab2b248dd3098eec297dc5aa3bbe6ada27273262b0b8b64",
    1_199_454_323,
)

_PROFILES: tuple[DeviceProfile, ...] = (
    DeviceProfile(
        id="iphone-duo",
        label="iPhone Duo",
        category="foldable",
        model="iPhone19,4",
        os_version="27.1",
        width=1398,
        height=2034,
        scale=3,
        pixels_per_inch=460,
        upstream_linux_tested=True,
        main_firmware=_firmware(
            "iPhone19,4_27.1_24A94425_Restore.ipsw",
            "https://updates.cdn-apple.com/2026FallFCS/"
            "3f41a447-d8a6-48ae-83ae-056a64f57fd4/"
            "iPhone19,4_27.1_24A94425_Restore.ipsw",
            "58fbfda85615e90221136d55288f64c9f41386510f8cd77feb8c9d20cc0a782e",
            12_724_731_538,
        ),
        cloud_os_firmware=_CLOUD_OS,
    ),
    DeviceProfile(
        id="iphone-16",
        label="iPhone 16",
        category="phone",
        model="iPhone17,3",
        os_version="27.0.1",
        width=1179,
        height=2556,
        scale=3,
        pixels_per_inch=460,
        upstream_linux_tested=True,
        main_firmware=_firmware(
            "iPhone17,3_27.0.1_24A446_Restore.ipsw",
            "https://updates.cdn-apple.com/2026FallFCS/"
            "38dca0ee-bb5d-4132-ad13-62d57bcd6d32/"
            "iPhone17,3_27.0.1_24A446_Restore.ipsw",
            "fc87bb9da8c03d0154d87531950f4a170195f6663ee5208653fa7a26bafb9469",
            12_343_857_610,
        ),
        cloud_os_firmware=_CLOUD_OS,
    ),
    DeviceProfile(
        id="ipad-mini-a17-pro",
        label="iPad mini (A17 Pro)",
        category="tablet",
        model="iPad16,1",
        os_version="27.0.1",
        width=1488,
        height=2266,
        scale=2,
        pixels_per_inch=326,
        upstream_linux_tested=False,
        main_firmware=_firmware(
            "iPad16,1,iPad16,2_27.0.1_24A446_Restore.ipsw",
            "https://updates.cdn-apple.com/2026FallFCS/"
            "46399de6-53d0-47ec-baea-c203630bcee9/"
            "iPad16,1,iPad16,2_27.0.1_24A446_Restore.ipsw",
            "8279171abaa2627899cc25d120cbb5d52a985c81a00396a4d202703c45fca8c9",
            11_759_417_454,
        ),
        cloud_os_firmware=_CLOUD_OS,
    ),
)
_PROFILES_BY_ID = {profile.id: profile for profile in _PROFILES}
_PROFILES_BY_MODEL_AND_OS = {(profile.model, profile.os_version): profile for profile in _PROFILES}


def get_profile(profile_id: str) -> DeviceProfile:
    """Return the named profile or raise ``ValueError`` for an unknown ID."""
    try:
        return _PROFILES_BY_ID[profile_id]
    except KeyError as exc:
        raise ValueError(f"unknown profile: {profile_id}") from exc


def profile_for(model: str, os_version: str) -> DeviceProfile:
    """Return the profile matching an exact product type and OS version."""
    try:
        return _PROFILES_BY_MODEL_AND_OS[(model, os_version)]
    except KeyError as exc:
        raise ValueError(f"no profile for {model} and OS {os_version}") from exc


def list_profiles() -> tuple[DeviceProfile, ...]:
    """Return the profiles in stable display order."""
    return _PROFILES


def public_metadata(profile: DeviceProfile) -> PublicProfileMetadata:
    """Serialize display-safe profile facts without firmware URLs or digests."""
    return {
        "id": profile.id,
        "label": profile.label,
        "category": profile.category,
        "model": profile.model,
        "os_version": profile.os_version,
        "width": profile.width,
        "height": profile.height,
        "scale": profile.scale,
        "pixels_per_inch": profile.pixels_per_inch,
        "upstream_linux_tested": profile.upstream_linux_tested,
        "firmware_metadata_ready": profile.firmware_metadata_ready,
    }
