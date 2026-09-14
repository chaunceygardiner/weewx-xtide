---
title: Installation
layout: default
nav_order: 2
description: Build XTide from source (libtcd, tide, harmonics data), verify it runs as the WeeWX user, then install the weewx-xtide extension.
---

# Installing weewx-xtide

[weewx-xtide manual](https://chaunceygardiner.github.io/weewx-xtide/) ·
[weewx-xtide on GitHub](https://github.com/chaunceygardiner/weewx-xtide) ·
[Report an issue](https://github.com/chaunceygardiner/weewx-xtide/issues)

---

Installation is two independent steps: build and install **XTide** (the
`tide` program and its harmonics data), then install the **weewx-xtide
extension**.  Do not proceed to the extension until `tide` runs.

## 1. Build and install XTide

Execute the following commands (it's probably easier to save them to a file
and run it as a script):

```sh
# Install dependencies
sudo apt install build-essential libpng-dev

# Download and install libtcd
cd /tmp
wget https://flaterco.com/files/xtide/libtcd-2.2.7-r3.tar.xz
tar xf libtcd-2.2.7-r3.tar.xz
cd libtcd-2.2.7
./configure
make
sudo make install
sudo ldconfig

# Download and build xtide
cd /tmp
wget https://flaterco.com/files/xtide/xtide-2.16.tar.xz
tar xf xtide-2.16.tar.xz
cd xtide-2.16
./configure --without-x --disable-shared CPPFLAGS="-I/usr/local/include" LDFLAGS="-L/usr/local/lib"
make
sudo make install

# Download harmonics data
cd /tmp
wget https://flaterco.com/files/xtide/harmonics-dwf-20251228-free.tar.xz
tar xf harmonics-dwf-20251228-free.tar.xz
cd harmonics-dwf-20251228
sudo mkdir -p /usr/local/share/xtide
sudo cp harmonics-dwf-20251228-free.tcd /usr/local/share/xtide/

# Create conf file
echo "/usr/local/share/xtide" | sudo tee /etc/xtide.conf
```

Verify that xtide works **as the user that WeeWX runs under**:

```sh
/usr/local/bin/tide -l "Palo Alto Yacht Harbor"
```

If that prints tide predictions, XTide is ready.

### Stations outside the US

Flater's harmonics file covers the United States only.  For anywhere
else, [openwatersio/tide-database](https://github.com/openwatersio/tide-database)
publishes XTide-compatible files that combine NOAA's US data with the
roughly 4,200 stations worldwide of TICON-4, a new release each month.
Download one from its
[releases page](https://github.com/openwatersio/tide-database/releases):
`neaps-<date>-metric.tcd` stores levels in meters and
`neaps-<date>-imperial.tcd` in feet.  Either works, because your reports
show levels in their own units whichever file you choose.

Install it **in place of** the free file, not beside it: with both in
`/usr/local/share/xtide`, US stations are listed twice, under two
different names.  Substitute the latest release's date:

```sh
sudo rm -f /usr/local/share/xtide/harmonics-dwf-*-free.tcd
sudo curl -L -o /usr/local/share/xtide/neaps-20260907-metric.tcd \
    https://github.com/openwatersio/tide-database/releases/download/v0.9.20260907/neaps-20260907-metric.tcd
```

`/etc/xtide.conf` already names that directory, so nothing else changes.
XTide also reads an `HFILE_PATH` environment variable, but WeeWX running
as a service never sees one set in your shell, so use the conf file.

Station names are not the same as in Flater's file (Palo Alto Yacht
Harbor is `Palo Alto Yacht Harbor, CA, United States` there), so look
yours up in the file you installed:

```sh
/usr/local/bin/tide -m l | grep -i brest
```

Then run the verification command above with that name, and do not skip
it: some stations in this file, subordinate stations especially, stop
tide with `XTide Fatal Error: UNRECOGNIZED_UNITS`.  Palo Alto Yacht
Harbor is one of them as of the 2026-09-07 release.  Choose a station
that prints predictions.

## 2. Install the extension

1. Activate the WeeWX virtual environment (actual syntax varies by type of
   WeeWX install):
   ```sh
   . /home/weewx/weewx-venv/bin/activate
   ```

1. Download `weewx-xtide.zip` from the latest release on the
   [releases page](https://github.com/chaunceygardiner/weewx-xtide/releases).

1. Install it.

   On a pip install `weectl` lives in the virtual environment, so
   activate it first (yours may sit elsewhere; `~/weewx-venv` is the usual
   place):

   ```sh
   source ~/weewx-venv/bin/activate
   weectl extension install weewx-xtide.zip
   ```

   On a Debian or Red Hat package install there is no environment to
   activate and `weectl` is already on the path:

   ```sh
   weectl extension install weewx-xtide.zip
   ```

   No `sudo`: that install put your account in the `weewx` group, which
   owns the files.  If you installed WeeWX in this same login session, log
   out and back in first so the group membership takes effect.

1. Set `location` and `prog` in weewx.conf — see
   [Configuration](configuration.md).

1. Restart WeeWX.  After the next reporting cycle, navigate to
   `<weewx-html-directory>/xtide` for the tide page.

## Upgrading

The same `weectl extension install` upgrades in place; no weewx.conf changes
are needed.  Note that upgrading replaces the bundled skin
(`skins/xtide/`) — if you customized it, save a copy first.
