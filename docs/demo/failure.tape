Output docs/assets/agentbarrier-demo.gif

Require uv

Set Shell "bash"
Set FontFamily "Menlo"
Set FontSize 18
Set Width 1320
Set Height 620
Set Padding 24
Set Margin 24
Set MarginFill "#0b1020"
Set BorderRadius 10
Set WindowBar "Colorful"
Set WindowBarSize 38
Set Framerate 24
Set TypingSpeed 12ms
Set Theme {"background":"#0d1117","foreground":"#e6edf3","cursor":"#22d3ee","black":"#161b22","red":"#ff7b72","green":"#3fb950","yellow":"#d29922","blue":"#58a6ff","magenta":"#bc8cff","cyan":"#39c5cf","white":"#f0f6fc","brightBlack":"#6e7681","brightRed":"#ffa198","brightGreen":"#56d364","brightYellow":"#e3b341","brightBlue":"#79c0ff","brightMagenta":"#d2a8ff","brightCyan":"#56d4dd","brightWhite":"#ffffff"}

Hide
Type "clear"
Enter
Show

Type "uv run python -m examples.run_unsafe_approval"
Enter
Wait+Screen@15s /Summary: 0 passed, 1 failed/
Sleep 2s

Type "uv run agentbarrier self-test --scenario approval_hold --color always"
Enter
Wait+Screen@15s /Summary: 1 passed, 0 failed/
Sleep 3s
