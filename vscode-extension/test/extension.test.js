"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const registered = new Map();
let activeRoot;

class MarkdownString {
  constructor(value = "") {
    this.value = value;
  }

  appendText(value) {
    this.value += value;
  }

  appendCode(value) {
    this.value += `\`${value}\``;
  }

  appendCodeblock(value, language) {
    this.value += `\n\`\`\`${language}\n${value}\n\`\`\``;
  }
}

class LanguageModelTextPart {
  constructor(value) {
    this.value = value;
  }
}

class LanguageModelToolResult {
  constructor(content) {
    this.content = content;
  }
}

const document = {
  uri: { fsPath: path.join(os.tmpdir(), "lm-agent-chat-test", "main.js") },
  languageId: "javascript",
  getText(selection) {
    return selection ? "selected text" : "const answer = 42;";
  }
};

const vscode = {
  MarkdownString,
  LanguageModelTextPart,
  LanguageModelToolResult,
  lm: {
    registerTool(name, implementation) {
      registered.set(name, implementation);
      return { dispose() {} };
    }
  },
  workspace: {
    getConfiguration() {
      return { get: (key, fallback) => key === "bridgePort" ? 0 : fallback };
    },
    get workspaceFolders() {
      return [{ uri: { fsPath: activeRoot } }];
    },
    getWorkspaceFolder() {
      return { uri: { fsPath: activeRoot } };
    }
  },
  window: {
    activeTextEditor: {
      document,
      selection: { isEmpty: false }
    },
    onDidCloseTerminal() {
      return { dispose() {} };
    },
    showWarningMessage() {},
    showInformationMessage() {}
  },
  commands: {
    registerCommand(name, handler) {
      registeredCommands.set(name, handler);
      return { dispose() {} };
    }
  }
};
const registeredCommands = new Map();

const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return vscode;
  return originalLoad.call(this, request, parent, isMain);
};
const extension = require("../extension");
Module._load = originalLoad;

test("registers the manifest tools and exposes editor context", async t => {
  activeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "lm-agent-chat-test-"));
  document.uri = { fsPath: path.join(activeRoot, "main.js") };
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  const profile = fs.readFileSync(
    path.join(__dirname, "..", "..", ".github", "agents", "lm-agent.agent.md"),
    "utf8"
  );
  const packagedProfile = fs.readFileSync(
    path.join(__dirname, "..", "agent-profile", "lm-agent.agent.md"),
    "utf8"
  );
  assert.equal(packagedProfile, profile);
  const extensionId = `${manifest.publisher}.${manifest.name}`;
  for (const tool of manifest.contributes.languageModelTools) {
    assert.ok(profile.includes(`${extensionId}/${tool.name}`), `agent profile is missing ${tool.name}`);
  }
  const previousCopilotHome = process.env.COPILOT_HOME;
  const context = {
    subscriptions: [],
    extensionPath: path.join(__dirname, ".."),
    globalState: { get: () => undefined }
  };
  t.after(() => {
    for (const disposable of context.subscriptions.reverse()) disposable.dispose();
    if (previousCopilotHome === undefined) delete process.env.COPILOT_HOME;
    else process.env.COPILOT_HOME = previousCopilotHome;
    fs.rmSync(activeRoot, { recursive: true, force: true });
  });

  extension.activate(context);
  await new Promise(resolve => setTimeout(resolve, 10));

  const contributed = manifest.contributes.languageModelTools.map(tool => tool.name);
  assert.deepEqual([...registered.keys()], contributed);
  assert.ok(registeredCommands.has("lmAgent.installChatProfile"));

  const contextTool = registered.get("lm-agent-vscode_get_context");
  const result = await contextTool.invoke(
    { input: { include_selection: true } },
    { isCancellationRequested: false }
  );
  const parsed = JSON.parse(result.content[0].value);
  assert.equal(parsed.path, "main.js");
  assert.equal(parsed.text, "const answer = 42;");
  assert.equal(parsed.selection, "selected text");

  const editTool = registered.get("lm-agent-vscode_edit_file");
  const prepared = await editTool.prepareInvocation({ input: { path: "src/main.js" } });
  assert.equal(prepared.confirmationMessages.title, "Replace a project file");
  assert.ok(prepared.confirmationMessages.message.value.includes("src/main.js"));

  const copilotHome = path.join(activeRoot, ".copilot");
  process.env.COPILOT_HOME = copilotHome;
  await registeredCommands.get("lmAgent.installChatProfile")();
  const installedProfile = path.join(copilotHome, "agents", "lm-agent.agent.md");
  assert.equal(fs.readFileSync(installedProfile, "utf8"), profile);

  fs.writeFileSync(installedProfile, "user profile");
  await registeredCommands.get("lmAgent.installChatProfile")();
  assert.equal(fs.readFileSync(installedProfile, "utf8"), "user profile");

  await assert.rejects(
    registered.get("lm-agent-vscode_create_file").invoke(
      { input: { path: "large.txt", content: "x".repeat(1_000_001) } },
      { isCancellationRequested: false }
    ),
    /byte limit/
  );
});
