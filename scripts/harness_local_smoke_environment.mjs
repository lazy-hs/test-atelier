// A loopback model smoke must not receive credentials or Node preloads from
// the invoking shell, even when the selected provider is overridden locally.
export function localSmokeEnvironment(source = process.env) {
  const allowed = [
    'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR', 'TEMP', 'TMP',
    'SystemRoot', 'WINDIR', 'ComSpec', 'PATHEXT', 'USERPROFILE', 'APPDATA',
    'LOCALAPPDATA', 'XDG_RUNTIME_DIR', 'XDG_CACHE_HOME', 'COREPACK_HOME',
    'PLAYWRIGHT_BROWSERS_PATH', 'CI', 'NO_COLOR', 'FORCE_COLOR',
  ]
  return Object.fromEntries(allowed.filter(name => typeof source[name] === 'string')
    .map(name => [name, source[name]]))
}
