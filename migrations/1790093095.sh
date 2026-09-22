echo "Installing driveinfo Nautilus extension"

extensions_dir="$HOME/.local/share/nautilus-python/extensions"
dest="$extensions_dir/driveinfo.py"
source="$OMARCHY_PATH/default/nautilus-python/extensions/driveinfo.py"

[[ -e $dest ]] && exit 0

mkdir -p "$extensions_dir"
cp "$source" "$dest"
chmod 644 "$dest"
