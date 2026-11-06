import pytest

from limulator.profiles import FirmwareDescriptor, get_profile, list_profiles, profile_for, public_metadata


@pytest.mark.parametrize(
    ("profile_id", "model", "os_version", "points", "upstream_linux_tested"),
    [
        ("iphone-duo", "iPhone19,4", "27.1", (466, 678), True),
        ("iphone-16", "iPhone17,3", "27.0.1", (393, 852), True),
        ("ipad-mini-a17-pro", "iPad16,1", "27.0.1", (744, 1133), False),
    ],
)
def test_profiles_resolve_exact_identity_and_native_points(
    profile_id, model, os_version, points, upstream_linux_tested
):
    profile = get_profile(profile_id)

    assert profile_for(model, os_version) is profile
    assert profile.points == points
    assert profile.upstream_linux_tested is upstream_linux_tested
    assert profile.firmware_metadata_ready


def test_catalog_has_three_stable_profiles_and_correct_product_labels():
    assert [profile.id for profile in list_profiles()] == [
        "iphone-duo",
        "iphone-16",
        "ipad-mini-a17-pro",
    ]
    assert get_profile("iphone-16").label == "iPhone 16"
    assert get_profile("ipad-mini-a17-pro").label == "iPad mini (A17 Pro)"


@pytest.mark.parametrize(
    ("model", "os_version"),
    [("iPhone17,3", "27.1"), ("unknown", "27.0.1")],
)
def test_lookup_requires_exact_model_and_os_pair(model, os_version):
    with pytest.raises(ValueError, match="no profile for"):
        profile_for(model, os_version)


def test_unknown_profile_id_is_reported():
    with pytest.raises(ValueError, match="unknown profile: missing"):
        get_profile("missing")


def test_public_metadata_does_not_expose_firmware_sources():
    profile = get_profile("iphone-duo")

    metadata = public_metadata(profile)

    assert metadata["id"] == "iphone-duo"
    assert metadata["width"] == 1398
    assert metadata["height"] == 2034
    assert metadata["firmware_metadata_ready"] is True
    assert "main_firmware" not in metadata
    assert "cloud_os_firmware" not in metadata
    assert "url" not in metadata
    assert "sha256" not in metadata


def test_unavailable_firmware_metadata_is_explicit_and_never_ready():
    profile = get_profile("iphone-16")
    unavailable = FirmwareDescriptor.unavailable()
    partial = FirmwareDescriptor("restore.ipsw", None, None, None, "unverified")

    assert unavailable.metadata_status == "unavailable"
    assert not unavailable.metadata_ready
    assert partial.filename == "restore.ipsw"
    assert partial.sha256 is None
    assert not partial.metadata_ready
    assert profile.main_firmware.metadata_ready


def test_firmware_metadata_validation_rejects_empty_or_incomplete_ready_descriptors():
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        FirmwareDescriptor("restore.ipsw", "https://example.com/restore.ipsw", "", 100, "unverified")
    with pytest.raises(ValueError, match="requires filename, URL, SHA-256 and size"):
        FirmwareDescriptor("restore.ipsw", None, None, None, "verified")
