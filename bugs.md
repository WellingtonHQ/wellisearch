# Bug log

[Nit] _visible_text_markdown emits only leaf elements, contradicting its docstring
Why it matters: The loop skips any element that has even one nested child (el.find(True) is not None → continue), so a mixed-content non-leaf like <p>prefix <a>x</a></p> contributes only "x" — the direct text "prefix" is silently lost. The docstring claims it emits "each element that contains only inline children (no nested block elements)" as one line, which the code does not do.