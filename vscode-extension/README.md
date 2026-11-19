# LM Agent for VS Code

Adds LM Agent editor tools to VS Code Chat and provides the local bridge used by
the LM Agent web chat.

## Requirements

- VS Code 1.100 or newer.
- GitHub Copilot Chat for the workspace agent profile and chat tools.

## Try it from this repository

Open the repository folder in VS Code and press **F5**. The configured
Extension Development Host opens with the extension enabled. Select **LM Agent**
from the chat agent picker. The workspace agent profile is in
`.github/agents/lm-agent.agent.md`.

To make the profile available in other workspaces, run **LM Agent: Install
Copilot Chat Agent** from the Command Palette. It installs the profile under
`$COPILOT_HOME/agents` (or `~/.copilot/agents` by default) and asks before
replacing an existing profile.

The chat uses the model selected in VS Code. The extension adds tools for the
active editor, Problems, project and analysis folders, project files, and the
VS Code terminal. VS Code manages confirmations for tool calls, especially file
edits, file creation, and terminal commands.

## Install a VSIX

In VS Code, open the Extensions view, select **...**, then **Install from VSIX...**
and choose the packaged `lm-agent-vscode-0.2.0.vsix` file.

The `LM Agent` profile is available when the LM Agent repository is open or
after running the install command above. The extension tools can also be used
from other Copilot Chat agents.

## Web chat bridge

To let the LM Agent web chat use VS Code, run the web agent on the same computer
as VS Code and keep this extension active. The bridge listens only on loopback.
Its port and token path are configurable through `lmAgent.bridgePort` and
`lmAgent.tokenFile`.
