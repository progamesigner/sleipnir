# Run once, after user dotfiles have configured PATH and the prompt.
if [[ -o interactive && -n $HC_TERMINAL_ID && $SLEIPNIR_ENABLE_AGENT_RESUME == 1 ]]; then
    _sleipnir_agent_resume() {
        add-zsh-hook -d precmd _sleipnir_agent_resume
        unfunction _sleipnir_agent_resume
        if [[ $SLEIPNIR_ENABLE_AGENT_RESUME == 1 && $AGENT_RESUME_AUTO != 0 ]]; then
            /usr/local/bin/sleipnir-agent-resume here || /usr/local/bin/sleipnir-agent-resume after $?
        fi
    }
    autoload -Uz add-zsh-hook
    add-zsh-hook precmd _sleipnir_agent_resume
fi
