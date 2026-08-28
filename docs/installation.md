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
wget https://flaterco.com/files/xtide/libtcd-2.2.7-r2.tar.bz2
tar xf libtcd-2.2.7-r2.tar.bz2
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

## 2. Install the extension

1. Activate the WeeWX virtual environment (actual syntax varies by type of
   WeeWX install):
   ```sh
   . /home/weewx/weewx-venv/bin/activate
   ```

1. Download `weewx-xtide.zip` from the latest release on the
   [releases page](https://github.com/chaunceygardiner/weewx-xtide/releases).

1. Install it:
   ```sh
   weectl extension install weewx-xtide.zip
   ```

1. Set `location` and `prog` in weewx.conf — see
   [Configuration](configuration.md).

1. Restart WeeWX.  After the next reporting cycle, navigate to
   `<weewx-html-directory>/xtide` for the tide page.

## Upgrading

The same `weectl extension install` upgrades in place; no weewx.conf changes
are needed.  Note that upgrading replaces the bundled skin
(`skins/xtide/`) — if you customized it, save a copy first.
