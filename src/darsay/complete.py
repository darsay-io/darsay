"""Shell completion scripts for the darsay CLI.

``darsay complete zsh`` (or bash, fish) prints a script to eval or save.
Bundle ids come from ``darsay list --ids``.
"""

from __future__ import annotations

COMMANDS = (
    "estimate",
    "archive",
    "verify",
    "smoke",
    "list",
    "info",
    "regen",
    "hydrate",
    "run",
    "dehydrate",
    "envs",
    "export",
    "import",
    "assemble",
    "rm",
    "du",
    "complete",
)
BUNDLE_COMMANDS = (
    "verify",
    "smoke",
    "info",
    "regen",
    "hydrate",
    "run",
    "dehydrate",
    "export",
    "rm",
)
def script_for(shell: str) -> str:
    if shell == "zsh":
        return _zsh()
    if shell == "bash":
        return _bash()
    if shell == "fish":
        return _fish()
    raise SystemExit(f"error: unknown shell {shell!r} (bash, zsh, or fish)")


def _zsh() -> str:
    cmds = " ".join(COMMANDS)
    bundle_case = "|".join(BUNDLE_COMMANDS)
    return f"""#compdef darsay
# eval "$(darsay complete zsh)"

_darsay_ids() {{
  local -a ids
  ids=(${{(f)"$(darsay list --ids 2>/dev/null)"}})
  _describe 'bundle' ids
}}

_darsay() {{
  local -a cmds
  cmds=({cmds})
  if (( CURRENT == 2 )); then
    _describe 'command' cmds
    return
  fi
  case $words[2] in
    {bundle_case})
      _darsay_ids
      ;;
    import)
      _files -g '*.mvb.tar'
      ;;
    assemble)
      _files -/
      ;;
    complete)
      _values 'shell' bash zsh fish
      ;;
    *)
      _files
      ;;
  esac
}}

compdef _darsay darsay
"""


def _bash() -> str:
    cmds = " ".join(COMMANDS)
    bundle_case = "|".join(BUNDLE_COMMANDS)
    return f"""# eval "$(darsay complete bash)"

_darsay() {{
  local cur
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  cmds="{cmds}"
  if [[ ${{COMP_CWORD}} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "${{cmds}}" -- "${{cur}}") )
    return 0
  fi
  case "${{COMP_WORDS[1]}}" in
    {bundle_case})
      local ids
      ids="$(darsay list --ids 2>/dev/null)"
      COMPREPLY=( $(compgen -W "${{ids}}" -- "${{cur}}") )
      ;;
    import)
      COMPREPLY=( $(compgen -f -X '!*.mvb.tar' -- "${{cur}}") )
      ;;
    complete)
      COMPREPLY=( $(compgen -W "bash zsh fish" -- "${{cur}}") )
      ;;
  esac
}}

complete -F _darsay darsay
"""


def _fish() -> str:
    cmds = " ".join(COMMANDS)
    bundle = " ".join(BUNDLE_COMMANDS)
    return f"""# darsay complete fish > ~/.config/fish/completions/darsay.fish

complete -c darsay -n "__fish_use_subcommand" -a "{cmds}"
complete -c darsay -n "__fish_seen_subcommand_from {bundle}" -a "(darsay list --ids 2>/dev/null)"
complete -c darsay -n "__fish_seen_subcommand_from import" -F -a "*.mvb.tar"
complete -c darsay -n "__fish_seen_subcommand_from complete" -a "bash zsh fish"
"""
