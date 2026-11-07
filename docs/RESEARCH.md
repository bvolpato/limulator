# Research: local iPhone execution on Linux

Research date: November 6, 2026. The target host is x86_64 Linux, an AMD Ryzen 9 9950X3D with an NVIDIA RTX 5070 Ti. The required workflow is xtool build, local app execution, interaction, screenshots, and automated checks.

## Decision

Use [vphone-linux](https://github.com/YahiaAngelo/vphone-linux) as the execution backend. Its pinned Linux-tested pairings include `iPhone17,3` with iOS 27.0.1 and iPhone Duo with iOS 27.1. Its [app-development workflow](https://github.com/YahiaAngelo/vphone-linux/blob/c2dc1996c8398ddb994288a29a719360f354d268/docs/APP-DEVELOPMENT.md) documents installing and running an unsigned IPA built with xtool. The guest daemon signs the development app inside the research guest. UIKit and SwiftUI execute inside iOS.

The Linux compatibility table mislabels `iPhone17,3` as a Pro and `iPad16,1` as an 11-inch Pro. The [pinned native device definitions](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/VPhoneKit/VPhoneCoreKit/VirtualMachine/VPhoneGuestDevice.swift) identify them as the base iPhone 16 and iPad mini (A17 Pro). Limulator uses those corrected identities and display geometry. The iPad pairing is upstream Mac-tested and starts experimental on Linux; acceptance must establish its native UIKit `pad` idiom and screen dimensions.

Duo has one visible cover framebuffer. The pinned guest components provide an experimental software inner UIKit display and closed, book, and open postures. They do not reproduce a physical hinge or a second physical scanout. The [guest HTTP API](https://github.com/Lakr233/vphone-cli/blob/83197152137ddd2aad70eb121d6b88a9afbee36e/Research/vphoned_http_api.md) documents posture and orientation readback. Actual frames and touch checks must validate the Linux behavior.

Pin the source and its submodule commits. Do not infer the tested revision from branch names, screenshots, or older prose in the upstream documentation. Build and runtime results on this workstation must establish our supported matrix.

## Alternatives

| Project | Verified upstream scope | Fit |
| --- | --- | --- |
| [vphone-linux](https://github.com/YahiaAngelo/vphone-linux) | Real ARM64 iOS guest on x86 Linux using QEMU TCG, Reims vGPU, metal2vulkan, guest RPC, touch, and screenshots | Selected. Modern apps and xtool workflow are documented. |
| [macbox](https://github.com/TUNA-NOPE/macbox) | Real Apple Simulator inside a local macOS VM, with a GPU translation path | Useful alternative, but its Intel guest is limited to iOS 26 and adds a macOS guest. |
| [Darling](https://github.com/darlinghq/darling) | Darwin/macOS compatibility layer, with ongoing GUI work | No complete iOS runtime or UIKit/SwiftUI execution path. |
| [touchHLE](https://github.com/touchHLE/touchHLE) | High-level framework emulation for early 32-bit iOS apps | Its explicit exclusion of 64-bit iOS rules it out for modern SwiftUI apps. |
| [ipasim](https://github.com/ipasimulator/ipasim) | Windows-oriented prototype for simple apps | Does not provide the required Linux runtime. |
| [Inferno](https://github.com/ChefKissInc/Inferno) | Apple ARM hardware emulation, established iPhone 11/iOS 14 research path | Important foundation, but the newer vphone stack is closer to the target OS and workflow. |
| [darwin-vm](https://github.com/jprx/darwin-vm) | Recent iOS and macOS kernels boot to a root shell | The project explicitly excludes graphics, GUI apps, and SpringBoard. A shell boot is insufficient. |
| [Corellium](https://support.corellium.com/troubleshooting-faqs/general-faqs) | Hosted virtual devices and an on-premises appliance | No downloadable local workstation hypervisor. |

## Evidence limits

The selected upstream reports one fully tested NVIDIA-equipped workstation. CPU instructions are translated under QEMU TCG, so timing differs from an iPhone. GPU translation has unsupported corners; upstream's census mode can continue with an inexact frame. Networking, audio, push, iCloud, and multiple simultaneous guests require separate validation. Installing a development IPA does not establish App Store or DRM compatibility.

Limulator uses strict graphics mode for new launches. Its explicit diagnostic mode enables census for inventory and must not serve as graphics acceptance evidence. Strict mode suppresses census and stops on the differences covered by Reims's strict policy; native-device relays and other precision or performance notices can remain. The [pinned mode implementation](https://github.com/YahiaAngelo/reims-vgpu/blob/960bf4b128a2678f032e46eb32964caa4828b818/crates/reims-vgpu-vulkan/src/answer.rs#L1683-L1695) defines that boundary. Device profiles select guest firmware and display geometry, not a corresponding physical Apple GPU. Neither strict mode nor a passing UI scenario establishes physical-device GPU equivalence. See [graphics policy setup](SETUP.md#choose-a-graphics-policy) for selection and recorded evidence.

The private Xcode 27.2 SDK supplies compilation interfaces and linker stubs. It does not supply a bootable guest OS. A successful Mach-O build therefore does not establish an execution environment.

Apple firmware is downloaded from its source into local storage. No Apple firmware or guest disk belongs in this repository. Upstream describes this runtime as a research and debugging environment and notes restrictions in Apple's software terms; its [license matrix](https://github.com/YahiaAngelo/vphone-linux/blob/main/docs/LICENSING.md) documents the separate open-source components.
