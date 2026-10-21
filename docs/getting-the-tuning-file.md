# Getting your tuning file

[README](../README.md) · [All docs](README.md)

The script looks for your laptop's speaker tuning by itself. With a Windows
partition mounted, run it with no path, as in the README's
[Quick start](../README.md#quick-start). With no Windows, pass your laptop's
audio driver download, and the run unpacks the tuning from it. Either way, it
picks the tuning that matches your audio hardware.

When it finds none, the guide for your tuning's kind says where else to look:

- [Extracting the XML](getting-the-xml.md) covers the Dolby DAX3 tuning XML. It
  says which driver downloads carry it, has a fetcher that downloads a Lenovo's
  for you, and shows how to pick the file by hand. It also covers the speaker
  tuning some Surface downloads carry beside the Dolby one.
- [Sonitude tunings](sonitude.md) covers a laptop whose driver carries no Dolby
  file, but a Sonitude speaker tuning instead. Support for it is experimental.

Start with the Dolby route if you don't know which your laptop ships.
