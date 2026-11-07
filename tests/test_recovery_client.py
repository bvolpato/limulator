"""Run the vendored C client lifecycle with USB setup failures under ASan."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(os.environ["LIMULATOR_BACKEND"]) if "LIMULATOR_BACKEND" in os.environ else None
VERSION = json.loads((ROOT / "third_party.lock.json").read_text())["vphone_cli"]["version"]
UPSTREAM = (
    Path(os.environ.get("VPHONE_CLI", BACKEND / "cpu-tree" / f"vphone-cli-{VERSION}")) if BACKEND else None
)
SOURCE = (
    UPSTREAM / "VPhoneExecutable/VPhoneCommand/VPhoneRestore/MobileRecoveryCore/libirecovery.c"
    if UPSTREAM
    else None
)

HARNESS = r"""
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libirecovery.h"
typedef void libusb_device_handle;
__CLIENT_STRUCT__
#define KIS_PRODUCT_ID 0x1881
#define debug(...) ((void)0)
#define sleep(seconds) ((void)0)
static int libirecovery_debug;
static int fail_configuration, failures, opened, closed, connected;
static irecv_client_t *output_slot;
static int on_connect(irecv_client_t client, const irecv_event_t *event) {
    assert(*output_slot == client && event->type == IRECV_CONNECTED);
    connected++;
    return 0;
}
static irecv_error_t libusb_open_with_ecid(irecv_client_t *client, uint64_t ecid) {
    *client = calloc(1, sizeof(**client));
    assert(*client);
    (*client)->handle = (void *)1;
    (*client)->mode = IRECV_K_RECOVERY_MODE_2;
    (*client)->device_info.ecid = ecid;
    (*client)->device_info.srnm = strdup("serial");
    (*client)->connected_callback = on_connect;
    opened++;
    return IRECV_E_SUCCESS;
}
static int libusb_release_interface(libusb_device_handle *handle, int interface) { return 0; }
static void libusb_close(libusb_device_handle *handle) { closed++; }
void irecv_set_debug_level(int level) {}
irecv_error_t irecv_usb_set_configuration(irecv_client_t client, int configuration) {
    return fail_configuration && failures-- > 0 ? IRECV_E_USB_CONFIGURATION : IRECV_E_SUCCESS;
}
irecv_error_t irecv_usb_set_interface(irecv_client_t client, int interface, int alternate) {
    return !fail_configuration && failures-- > 0 ? IRECV_E_USB_INTERFACE : IRECV_E_SUCCESS;
}
static irecv_error_t irecv_kis_init(irecv_client_t client) { return IRECV_E_SUCCESS; }
static irecv_error_t irecv_kis_load_device_info(irecv_client_t client) { return IRECV_E_SUCCESS; }
static void irecv_copy_nonce_with_tag(irecv_client_t client, const char *tag,
                                    unsigned char **nonce, unsigned int *size) {}
__FUNCTIONS__
int main(int argc, char **argv) {
    irecv_client_t client = NULL;
    output_slot = &client;
    fail_configuration = atoi(argv[1]);
    failures = atoi(argv[2]);
    irecv_error_t result = irecv_open_with_ecid_and_attempts(&client, 42, 2);
    if (failures > 0) {
        assert(result == IRECV_E_UNABLE_TO_CONNECT && client == NULL && connected == 0);
    } else {
        assert(result == IRECV_E_SUCCESS && client != NULL && connected == 1);
        irecv_close(client);
    }
    assert(opened == 2 && closed == 2);
    return 0;
}
"""


def lifecycle_harness(source):
    client = re.search(r"^struct irecv_client_private \{.*?^};", source, re.M | re.S)
    assert client is not None
    functions = []
    for name in ("irecv_cleanup", "irecv_close", "irecv_open_with_ecid", "irecv_open_with_ecid_and_attempts"):
        match = re.search(rf"^(?:static )?irecv_error_t {name}\([^;]*?\n\{{.*?^}}", source, re.M | re.S)
        assert match is not None, name
        functions.append(match.group())
    return HARNESS.replace("__CLIENT_STRUCT__", client.group()).replace("__FUNCTIONS__", "\n".join(functions))


@pytest.mark.parametrize("configuration,failures", [(0, 1), (1, 1), (0, 3)])
def test_setup_failure_retries_without_reusing_freed_client(tmp_path, configuration, failures):
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None or BACKEND is None:
        pytest.skip("an explicitly selected LIMULATOR_BACKEND fork checkout and a C compiler are required")
    assert SOURCE is not None and SOURCE.is_file(), "the selected CLI fork is missing libirecovery.c"

    # Exercise the same edits and source sync used by bootstrap_backend.sh cli.
    build = tmp_path / "linux-create"
    build.mkdir()
    for directory in ("edits", "excludes", "overlay"):
        shutil.copytree(BACKEND / "linux-create" / directory, build / directory)
    shutil.copy2(BACKEND / "linux-create/sync-sources.sh", build)
    subprocess.run(
        ["bash", str(build / "sync-sources.sh")],
        env={**os.environ, "VPHONE_CLI": str(UPSTREAM)},
        check=True,
        capture_output=True,
        text=True,
    )

    harness = tmp_path / "recovery.c"
    harness.write_text(lifecycle_harness((build / "Sources/MobileRecoveryCore/libirecovery.c").read_text()))
    executable = tmp_path / "recovery"
    subprocess.run(
        [
            compiler,
            "-g",
            "-fsanitize=address",
            "-fno-omit-frame-pointer",
            "-I",
            str(SOURCE.parent / "Include"),
            str(harness),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [str(executable), str(configuration), str(failures)],
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
