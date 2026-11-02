# Run once, after user dotfiles have configured PATH and the prompt.
if [[ -o interactive && -n $HC_TERMINAL_ID && $SLEIPNIR_ENABLE_AGENT_RESUME == 1 ]]; then
    _sleipnir_agent_resume() {
        local resume_code=0
        # A temporarily unavailable core must not consume the one-shot hook.
        (( SECONDS < ${_sleipnir_resume_retry_at:-0} )) && return 0
        if [[ $SLEIPNIR_ENABLE_AGENT_RESUME == 1 && $AGENT_RESUME_AUTO != 0 ]]; then
            /usr/local/bin/sleipnir-agent-resume here || resume_code=$?
            if (( resume_code == 75 )); then
                typeset -g _sleipnir_resume_retry_at=$(( SECONDS + 30 ))
                return 0
            fi
            (( resume_code )) && /usr/local/bin/sleipnir-agent-resume after $resume_code
        fi
        add-zsh-hook -d precmd _sleipnir_agent_resume
        unfunction _sleipnir_agent_resume
        unset _sleipnir_resume_retry_at
    }
    autoload -Uz add-zsh-hook
    add-zsh-hook precmd _sleipnir_agent_resume
fi
