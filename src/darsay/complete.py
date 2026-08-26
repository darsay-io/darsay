"""Shell completion scripts for the darsay CLI.

``darsay complete zsh`` (or bash, fish) prints a script to eval or save.
Bundle ids come from ``darsay list --ids``.
"""

from __future__ import annotations

COMMANDS = (
    "estimate archive verify smoke list info regen hydrate run dehydrate "
    "envs export import assemble rm du complete"
)
BUNDLE_COMMANDS = (
    "verify smoke info regen hydrate run dehydrate export rm"
)


def script_for(shell: str) -> str:
    if shell == "zsh":
        return _ZSH
    if shell == "bash":
        return _BASH
    if shell == "fish":
        return _FISH
    raise SystemExit(f"error: unknown shell {shell!r} (bash, zsh, or fish)")


_ZSH = r"""#compdef darsay
# eval "$(darsay complete zsh)"

_darsay_ids() {
  local -a ids
  ids=(${(f)"$(darsay list --ids 2>/dev/null)"})
  _describe 'bundle' ids
}

_darsay() {
  local -a cmds
  cmds=(estimate archive verify smoke list info regen hydrate run dehydrate envs export import assemble rm du complete)
  if (( CURRENT == 2 )); then
    _describe 'command' cmds
    return
  fi
  case $words[2] in
    verify|smoke|info|regen|hydrate|run|dehydrate|export|rm)
      _darsay_ids
      ;;
    complete)
      _values 'shell' bash zsh fish
      ;;
    *)
      _files
      ;;
  esac
}

compdef _darsay darsay
"""

_BASH = r"""# eval "$(darsay complete bash)"

_darsay() {
  local cur cmds
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  cmds="estimate archive verify smoke list info regen hydrate run dehydrate envs export import assemble rm du complete"
  if [[ ${COMP_CWORD} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "${cmds}" -- "${cur}") )
    return 0
  fi
  case "${COMP_WORDS[1]}" in
    verify|smoke|info|regen|hydrate|run|dehydrate|export|rm)
      local ids
      ids="$(darsay list --ids 2>/dev/null)"
      COMPREPLY=( $(compgen -W "${ids}" -- "${cur}") )
      ;;
    complete)
      COMPREPLY=( $(compgen -W "bash zsh fish" -- "${cur}") )
      ;;
  esac
}

complete -F _darsay darsay
"""

_FISH = r"""# darsay complete fish > ~/.config/fish/completions/darsay.fish

complete -c darsay -f
complete -c darsay -n "__fish_use_subcommand" -a "estimate archive verify smoke list info regen hydrate run dehydrate envs export import assemble rm du complete"
complete -c darsay -n "__fish_seen_subcommand_from verify smoke info regen hydrate run dehydrate export rm" -a "(darsay list --ids 2>/dev/null)"
complete -c darsay -n "__fish_seen_subcommand_from complete" -a "bash zsh fish"
"""
