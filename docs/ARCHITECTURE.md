# Architecture

## Required outcome

A developer on this Linux machine builds a real ARM64 iPhone app with xtool, installs it into a local iOS guest, launches it, interacts with its native controls, captures genuine guest frames, and runs checks that fail when the expected behavior is absent. The same runtime supports manual inspection through a local console.

## Components

```text
xtool + Apple SDK -> ARM64 .app / .ipa
                         |
Limulator CLI -----------+------ local web console
       |                           |
       +------ device service -----+
                    |
        +-----------+-------------+
        |                         |
   guest HTTP/RPC               QMP
   over local vsock       lifecycle / framebuffer
        |                         |
        +----- QEMU iOS guest ----+
                 |
       UIKit / SwiftUI / app
                 |
       Reims vGPU -> metal2vulkan -> host Vulkan GPU
```

The emulator and firmware remain separate from Limulator's Python service. Backend sources are pinned and built under the user's data directory. The application SDK can reuse an existing xtool installation. Source compilation, guest installation, app launch, and screen capture are separate operations with separate failure results.

## Device lifecycle

Each registered device records its actual VM path, model, OS version, vsock CID, and display information. Names and paths are validated. Only one process may own a VM disk. A held file lock and a live process identity protect against duplicate launches and PID reuse. Stopping a device must target the exact process Limulator started, never a name-based process sweep. A stale metadata file is not proof that a guest is live or stopped.

Profiles pin the product identity, firmware pair, native pixel dimensions, and scale. Creation applies these to the guest configuration and firmware preparation. Tablet acceptance requires the app to report the native UIKit `pad` idiom; resizing a phone framebuffer does not establish tablet support. Browser requests carry a device name and use a separate cached service for each device, so changing one tab's selection cannot redirect another tab's input.

QEMU's own disk locking is disabled by the upstream launcher, so Limulator must add an ownership lock and detect another process that already has the disk open. Restore and firmware mutation require an exclusive stopped guest. Resuming an interrupted creation must use the backend's real stage results and logs, not fabricate a successful runtime state.

## Control and capture

The guest daemon provides app installation, launch, unlock, text input, display posture, and device metadata. QMP provides framebuffer capture and a fallback input path. Protocol clients must validate response framing, bounded sizes, timeouts, HTTP status, RPC errors, and asynchronous QMP events. IPA uploads stream instead of loading the full archive into memory.

Capture requests an uncompressed PPM framebuffer from QEMU, then encodes the public PNG in the host service. This keeps PNG compression outside QEMU's global lock. Pixel values and the screenshot API remain lossless; capture latency and its effect on the guest still require runtime measurement.

Input uses the current screenshot dimensions or guest-reported point geometry. It must not assume a 430 by 932 display. A fold can change orientation, size, and coordinate mapping. Tap and swipe operations must release the pointer even when a later step fails.

A screenshot is a frame captured from the guest. Offline devices produce an explicit error and an empty console state. There are no cached stock screenshots, synthetic native screens, or silent image replacements. Render degradation and unsupported GPU features must remain observable.

## Application input

Inspect .app and .ipa metadata before upload. Accept a single real application bundle with a matching executable. Reject unsafe archive paths, encrypted Mach-O executables, simulator binaries, incompatible architecture, and an OS deployment floor newer than the guest. Package .app input into an IPA in a private temporary directory. Run xtool with an argument list and an explicit project directory; never interpolate a user-supplied command into a shell.

## Console boundary

The service binds to loopback by default. Its browser endpoints validate Host and Origin and require a per-session token for mutations. The browser cannot submit arbitrary shell commands or arbitrary guest RPC methods. Static assets are bundled locally. Apps and screenshots stay on this workstation.

## Test and evidence model

Protocol tests exercise real local sockets with fragmented and failed replies. Package tests use meaningful archive and Mach-O fixtures. Lifecycle tests protect disk ownership and stale-process branches. Console tests cover the API boundary and pointer mapping. These tests do not establish iOS compatibility.

Runtime acceptance requires an app built by xtool on this host, installed and launched in the real guest. Its counter, editable text, navigation, and persisted state must respond to actual input. Capture before and after frames, app/OS metadata, operation results, and hashes. Also test duplicate-start refusal, shutdown/restart, and a clear failure for an invalid app. Duo open, book, and closed postures require separate observed frames and input checks.

Passing a backend build, reaching a lock screen, comparing two different images, or rendering the console alone does not satisfy runtime acceptance. Physical-device timing, camera, biometric, radio, push, and App Store behavior remain outside the evidence a virtual guest can supply.
