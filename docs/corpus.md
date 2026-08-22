# The corpus — what the cross-device figures are measured over

> Interpretive analysis of the parameter schema used by Dolby DAX3 tuning XML (distributed publicly as part of Windows audio driver packages), for the purpose of Linux interoperability. No verbatim tuning arrays are reproduced.

[cross-device-findings.md](cross-device-findings.md) reports what is universal
across Dolby DAX3 tunings and what varies by device. Every figure in it is
measured over a collection of tuning XMLs assembled from OEM driver packages.
Those XMLs are third-party data this project does not own, so the collection is
not in the repository and will not be — which leaves the findings impossible to
check unless someone can tell what was in it.

This page is that description: how a file is counted, what the collection holds,
which download each part came from, and what it is skewed towards. It is not a
guide to obtaining tuning XMLs — the [README](../README.md#extracting-the-xml)
covers extracting the one for your own device.

> **Figures below are from a `tools/corpus_audit.py --composition` run on
> 2026-08-22**, and are re-derived on their own date. They will not match
> [cross-device-findings.md](cross-device-findings.md), which freezes its
> per-parameter figures against a dated cohort — see "Reconciling the counts".

## How a file is counted

A corpus XML is one per-SKU Dolby playback tuning. They are named after the
audio device they bind to, in one of two families:

- **HD Audio** — `DEV_<codec>_SUBSYS_<vendor><device>_PCI_SUBSYS_<device><vendor>.xml`.
  The same tuning also appears as `HDAUDIO_DEV_…`, `INTELAUDIO_DEV_…`,
  `PCI_DEV_…`, and as `AUCD_DEV_…_ADCM_SUBSYS_…` on Qualcomm Aqstic. Those
  prefixes are the Windows hardware-ID namespace the tuning's `.inf` binds it
  under, and the **OEM package ships them** — they are not produced by
  installing it. See [cross-device-findings.md](cross-device-findings.md#17-bus-prefixed-filenames-are-one-tuning-not-two)
  for the evidence and for what it means when two of them match one device.
- **SoundWire** — `SOUNDWIRE_[SDCAFUNCTION_NN_]MAN_<man>_FUNC_<func>_SUBSYS_<device><vendor>.xml`,
  and a shorter `SDW_…` spelling.

Three companions share that shape and are excluded: `_settings.xml` (UI and
profile defaults, no DSP), and `_dmic.xml` / `_amic.xml`, which are Dolby Fusion
microphone-AEC tunings rather than playback ones. What is left is what the
converter itself would accept — `is_dolby_tuning_filename` in
[`lib/dax/discover.py`](../lib/dax/discover.py) is the single definition, shared
by the converter's auto-discovery, `tests/corpus/`, and the sweep tool, so the
population the tests walk and the population the figures are measured over
cannot drift apart.

## What it holds

| | |
|---|---|
| Tuning XMLs | 3117 |
| Distinct tunings by content | 812 |
| Distinct filenames | 1040 |
| Distinct `SUBSYS` device ids | 856 |
| Profile rows (endpoint × operating mode × profile) | 46992 |
| Codec ids | 20 |
| Driver packages | 13 |

Per-codec counts and everything downstream of them are in
[cross-device-findings.md](cross-device-findings.md); this page does not repeat
them.

The gap between 3117 files and 812 distinct tunings is the shape of the data:
one tuning ships to every SKU it fits, in every package that supports that SKU.
The most-repeated tuning appears 54 times, and only 177 files are the sole copy
of their content. Counting files therefore overstates coverage by roughly 4×,
which is why the findings doc counts files, rows and devices separately rather
than quoting one number for a prevalence.

## Where the files come from

Every file has one of three origins:

| Source | Files | Distinct tunings |
|---|---|---|
| A publicly downloadable driver package | 2892 | 808 |
| The development machine's Windows partition | 219 | 202 |
| Attached to a GitHub issue | 6 | 6 |

The rows are disjoint and sum to the 3117 above; a file attached to an issue is
counted only there, never also as a package file.

Only the middle row is something nobody else can fetch — and it contributes
nothing that isn't fetchable anyway: **all 202 of its tunings also ship in one of
the public packages below.** What no download yields is four tunings, each
attached to an issue by the person reporting the device. Everything else here is
reproducible by anyone willing to pull the same packages.

### Publicly downloadable driver packages

Each was downloaded as a self-extracting installer from the vendor's support
site and unpacked with [`innoextract`](https://constexpr.org/innoextract/install).
The layout inside varies (`Source/Dolby/…`, `Source/ThirdParty/…`, `Dolby/…`,
and Samsung's `APO/Dolby/` with the `.inf` flat beside the tunings), so there is
no fixed path to them. The last column records **which download the package came
from**, not a claim about that model.

| Dolby package | XMLs | Source download | Downloaded for |
|---|---|---|---|
| `dax3_ext_cirrus` | 5 | `BASW-A4285A20_1063.ZIP` | Galaxy Book6 Pro (Samsung, Cirrus SoundWire) |
| `ext_24h2_v10.307.807.28` | 198 | `wplc2w0fah72yve0.exe` | IdeaPad 3 17ABA7 |
| `ext_ideapad_AIO_senary_21h2_22h2_v8.920.549.59` | 22 | `rwsa060fjbbg7kf0.exe` | IdeaPad Slim 5x Gen 9 |
| `ext_lenovo_AIO_rtk_19h1_20h1_v6.503.308.23` | 195 | `wesa04af40yga0.exe` | Yoga Slim 7 14ARE05 |
| `ext_lenovo_AIO_rtk_20h1_21h2_22h2_v8.426.733.17` | 460 | `eisp030f4l8vfkd0.exe` | Yoga Slim 7 ProX 14ARH7 |
| `ext_lenovo_AIO_rtk_22h2_24h2_25h2_v10.1029.1430.37` | 696 | `kkau100fq18jlle0.exe` | IdeaPad 5x 2-in-1 14 |
| `ext_lenovo_AIO_rtk_22h2_24h2_v10.725.730.25` | 655 | `14yo037flhg44zg0.exe` | Yoga 7 2-in-1 16AKP10 |
| `ext_qc_lenovo_thinkpad` | 2 | `n3ha810w.exe` | ThinkPad X13s Gen 1 (Qualcomm Aqstic) |
| `ext_realtek_lenovo_ideapad` | 60 | `mwy506af40hk90.exe` | Legion Y540-15IRH |
| `ext_thinkpad_AIO_rtk_19h1_20h1_v6.108.104.39` | 68 | `n2wa126w.exe` | ThinkPad X1 Carbon Gen 8 |
| `ext_thinkpad_AIO_rtk_20h1_22h2_24h2_v9.1127.1236.0` | 219 | `r2nao09w.exe` | ThinkPad T14s Gen 6 |
| `ext_thinkpad_AIO_rtk_22h2_24h2_25h2_v10.1022.826.17` | 243 | `n4kao13w.exe` | ThinkPad X13 Gen 6 |

The table sums to 2823. The other 69 files of this source are duplicate copies
held elsewhere in the working tree — a re-organised copy of the X1 Carbon
package, and staged copies left by a test harness — not additional tunings.

`ext_realtek_lenovo_ideapad` is not one folder: it holds fifteen per-model
subfolders, one per SKU that download covers. That layout is why it is the only
package here that ships two bus-prefixed spellings of the *same* device — see
[cross-device-findings.md](cross-device-findings.md#17-bus-prefixed-filenames-are-one-tuning-not-two).

Not every audio driver package carries a tuning, and for some vendors none of
the downloadable ones do: ASUS ships them through Windows Update only, which is
why its entry below arrived through an issue rather than as a package.

### The development machine's Windows partition

219 files, in the `dax3_ext_rtk.inf_amd64_*` package that Windows installed on
the ThinkPad X1 Yoga Gen 7 this project is developed on. Reachable only by
mounting that partition, which is why the corpus tier and the sweep both walk
NTFS mounts as well as directories you point them at. Nothing is lost by not
having it — Lenovo's downloadable packages carry all 202 of these tunings.

### Attached to a GitHub issue

Six files, six distinct tunings, four of which appear in no package here. These
arrive when someone reports a device whose vendor doesn't publish the tuning, or
whose driver this project has no download for.

| Device | Key | Issue |
|---|---|---|
| Apple Intel Mac, Boot Camp (×2) | `PCI_DEV_1803`, `106B` | [#21](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/21) |
| ASUS ROG Xbox Ally X | `DEV_0294`, `10431384` | [#39](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/39) |
| Lenovo on Qualcomm Aqstic | `AUCD_DEV_0C29`, `IDEA4002` | [#4](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/4) |
| ThinkPad E14 | `DEV_0257`, `17AA507F` | [#25](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/25) |
| Lenovo XiaoXin Pro 14GT 2026 | `DEV_0287`, `17AA3941` | [#67](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/67) |

## What it is skewed towards

- **One vendor.** 847 of the 856 device ids carry Lenovo's `17AA`. The other
  nine are five Samsung (`144D`) SoundWire endpoints, two Apple (`106B`), one
  ASUS (`1043`), and one Lenovo Qualcomm entry keyed `IDEA4002`. A finding that
  holds across the corpus is a finding that holds across *Lenovo's* tuning
  practice; it is evidence about the DAX3 schema, and much weaker evidence about
  what other OEMs do with it.
- **One endpoint.** Every row is `internal_speaker`. There are no headphone or
  external-output tunings in any package here, so nothing in the findings speaks
  to those.
- **Breadth by accident, not design.** Twelve downloads yield 856 device ids
  because a Lenovo audio package carries the tunings for every SKU it supports,
  not just the machine you downloaded it for. Coverage is therefore wide across
  SKUs and narrow across vendors, kernels, and codec generations.
- **Parameters, not sound.** These are declared values. Nothing here is a
  measurement of what a device actually produces; that comes from the on-device
  captures in [design-notes.md](design-notes.md).

## Comparing your own collection

Point the sweep at any directory of tuning XMLs — an extracted driver tree, a
mounted Windows `DriverStore`, or a hand-organised folder — and it prints the
same makeup block as the tables above:

```bash
python3 tools/corpus_audit.py --composition /path/to/xmls
```

Drop `--composition` for the full per-parameter sweep behind
[cross-device-findings.md](cross-device-findings.md), and see
[Running the tests](../README.md#running-the-tests) for pointing the
`tests/corpus/` tier at the same directory.

## Reconciling the counts

Corpus totals quoted around this repository differ, for three reasons worth
knowing before comparing any two of them:

1. **They are dated.** The collection grows as driver packages are pulled.
   Figures carry the date they were derived; the findings doc freezes a cohort
   and states which one.
2. **They may include the development machine's mounted Windows partition.**
   Both the sweep tool and `tests/corpus/` walk whatever roots they are given or
   auto-discover, and that partition holds an installed DAX3 package of its own.
   The figures on this page include it.
3. **The filter was wrong until 2026-08-09.** The sweep tool tested for a
   `DEV_`/`SOUNDWIRE`/`SDW` filename prefix of its own rather than the
   converter's definition. That counted the `_dmic`/`_amic` microphone
   companions as speaker tunings — six of the packages it listed turned out to
   hold nothing else — and skipped the `HDAUDIO_`/`INTELAUDIO_`/`PCI_`/`AUCD_`
   spellings entirely. On the current collection the correction removes 186
   microphone companions (plus one synthetic test fixture) and adds 176 speaker
   tunings carrying 2136 profile rows, a 4.8% increase in the analysed
   population. **Every file and row count in
   [cross-device-findings.md](cross-device-findings.md) predates the fix** and is
   pending re-derivation; the per-parameter distributions are computed over rows
   and shift only by whatever the added devices contribute.
