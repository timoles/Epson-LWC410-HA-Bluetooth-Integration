#!/bin/sh
# Copy the printer library and renderer (canonical source: python/) into the
# add-on build context. Run after changing python/lwc410_bt.py or
# python/render.py, then bump "version" in lwc410_print/config.yaml so Home
# Assistant offers the update.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cp "$here/python/lwc410_bt.py" "$here/python/render.py" "$here/lwc410_print/app/"
echo "synced lwc410_bt.py and render.py into lwc410_print/app/"
