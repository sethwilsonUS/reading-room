-- Reading Room.app entry point, compiled by scripts/build_mac_app.py.
-- An applet (not a shell-script executable) is required so macOS attributes
-- Documents-folder access to "Reading Room" and can ask for permission.
-- The launcher runs in the background so this applet quits immediately and
-- only the Reading Room window remains.
on run
	set launcher to POSIX path of (path to me) & "Contents/Resources/launch.sh"
	do shell script quoted form of launcher & " >/dev/null 2>&1 &"
end run
