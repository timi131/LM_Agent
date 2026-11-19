"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const vscode = require("vscode");

const MAX_BODY = 1_000_000;
const MAX_CONTEXT = 20_000;
const MAX_PROBLEMS = 200;
const MAX_FILE_READ = 20_000;

let extensionContext;
let projectRoot;
let analysisRoot;
let agentTerminals = new Map();

const chatTools = [
  { name: "lm-agent-vscode_get_context", endpoint: "context", label: "Reading the active editor" },
  { name: "lm-agent-vscode_get_problems", endpoint: "problems", label: "Reading VS Code problems" },
  { name: "lm-agent-vscode_select_project_folder", endpoint: "select_project", label: "Selecting a project folder" },
  { name: "lm-agent-vscode_select_analysis_folder", endpoint: "select_analysis", label: "Selecting an analysis folder" },
  {
    name: "lm-agent-vscode_list_files", endpoint: "list", label: "Listing project files",
  },
  {
    name: "lm-agent-vscode_read_file", endpoint: "read", label: "Reading a project file",
  },
  {
    name: "lm-agent-vscode_open_file", endpoint: "open", label: "Opening a project file",
  },
  {
    name: "lm-agent-vscode_create_file", endpoint: "create", label: "Creating a project file",
    confirmation: "Create a new project file"
  },
  {
    name: "lm-agent-vscode_edit_file", endpoint: "edit", label: "Editing a project file",
    confirmation: "Replace a project file"
  },
  {
    name: "lm-agent-vscode_run_terminal", endpoint: "terminal", label: "Sending a command to the VS Code terminal",
    confirmation: "Run a command in the VS Code terminal"
  }
];

function tokenFilePath() {
  const configured = vscode.workspace.getConfiguration("lmAgent").get("tokenFile");
  return configured.startsWith("~")
    ? path.join(os.homedir(), configured.slice(1).replace(/^[/\\]/, ""))
    : configured;
}

function isLoopback(address) {
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function send(response, status, value) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(value));
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", chunk => {
      body += chunk;
      if (Buffer.byteLength(body, "utf8") > MAX_BODY) {
        reject(new Error("Тело запроса слишком большое"));
        request.destroy();
      }
    });
    request.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch {
        reject(new Error("Некорректный JSON"));
      }
    });
    request.on("error", reject);
  });
}

function defaultProjectRoot() {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) throw new Error("Сначала выберите папку проекта в VS Code");
  return fs.realpathSync(folder.uri.fsPath);
}

function rootForScope(scope) {
  if (scope === "project") return projectRoot || defaultProjectRoot();
  if (scope === "analysis") {
    if (!analysisRoot) throw new Error("Сначала выберите папку для анализа через vscode_select_analysis_folder");
    return analysisRoot;
  }
  throw new Error("scope должен быть project или analysis");
}

function isWithin(root, target) {
  const relative = path.relative(root, target);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function resolveFolderPath(relativePath, scope, allowRoot = false, allowMissing = false) {
  if (typeof relativePath !== "string" || !relativePath.trim() || path.isAbsolute(relativePath)) {
    throw new Error("Путь должен быть непустым относительным путём внутри выбранной папки");
  }
  const root = fs.realpathSync(rootForScope(scope));
  const target = path.resolve(root, relativePath);
  if (!isWithin(root, target) || (!allowRoot && target === root)) {
    throw new Error("Путь находится вне выбранной папки");
  }

  const relative = path.relative(root, target);
  let current = root;
  for (const part of relative ? relative.split(path.sep) : []) {
    current = path.join(current, part);
    try {
      if (fs.lstatSync(current).isSymbolicLink()) throw new Error("Символические ссылки в выбранной папке запрещены");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      if (!allowMissing) throw new Error("Файл или папка не найдены");
    }
  }
  return { root, target, uri: vscode.Uri.file(target) };
}

async function selectFolder(scope) {
  const isProject = scope === "project";
  const current = isProject
    ? projectRoot || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath
    : analysisRoot || projectRoot || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const selected = await vscode.window.showOpenDialog({
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: isProject ? "Выбрать папку проекта" : "Выбрать папку для анализа",
    title: isProject ? "Папка проекта LM Agent" : "Папка для чтения и анализа",
    ...(current ? { defaultUri: vscode.Uri.file(current) } : {})
  });
  if (!selected?.length) return { cancelled: true };
  const selectedPath = fs.realpathSync(selected[0].fsPath);
  if (!fs.statSync(selectedPath).isDirectory()) throw new Error("Выберите папку");
  if (isProject) {
    projectRoot = selectedPath;
    await extensionContext.globalState.update("projectRoot", projectRoot);
  } else {
    analysisRoot = selectedPath;
    await extensionContext.globalState.update("analysisRoot", analysisRoot);
  }
  return { path: selectedPath, name: path.basename(selectedPath) };
}

function listFiles(relativePath, scope) {
  const { target } = resolveFolderPath(relativePath, scope, true);
  if (!fs.statSync(target).isDirectory()) throw new Error("Указанный путь не является папкой");
  const entries = fs.readdirSync(target, { withFileTypes: true })
    .filter(entry => !entry.name.startsWith(".") && !entry.isSymbolicLink())
    .map(entry => ({ name: entry.name, directory: entry.isDirectory() }))
    .sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name));
  return {
    files: entries.slice(0, 200).map(entry => `${entry.name}${entry.directory ? "/" : ""}`),
    truncated: entries.length > 200
  };
}

function readFile(relativePath, scope) {
  const { target } = resolveFolderPath(relativePath, scope);
  const stat = fs.statSync(target);
  if (!stat.isFile()) throw new Error("Указанный путь не является файлом");
  const descriptor = fs.openSync(target, "r");
  const buffer = Buffer.alloc(MAX_FILE_READ + 1);
  let bytesRead;
  try {
    bytesRead = fs.readSync(descriptor, buffer, 0, buffer.length, 0);
  } finally {
    fs.closeSync(descriptor);
  }
  const content = buffer.subarray(0, Math.min(bytesRead, MAX_FILE_READ));
  if (content.includes(0)) throw new Error("Бинарные файлы читать нельзя");
  return { content: content.toString("utf8"), truncated: stat.size > MAX_FILE_READ };
}

function createFile(relativePath, content) {
  if (typeof content !== "string") throw new Error("Поле content должно быть строкой");
  const { root, target } = resolveFolderPath(relativePath, "project", false, true);
  if (fs.existsSync(target)) throw new Error("Файл уже существует; используйте vscode_edit_file для замены");
  const parent = path.dirname(target);
  const relativeParent = path.relative(root, parent);
  let current = root;
  for (const part of relativeParent ? relativeParent.split(path.sep) : []) {
    current = path.join(current, part);
    try {
      if (fs.lstatSync(current).isSymbolicLink()) throw new Error("Символические ссылки в выбранной папке запрещены");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      fs.mkdirSync(current);
    }
  }
  fs.writeFileSync(target, content, { encoding: "utf8", flag: "wx" });
  return { path: path.relative(root, target) };
}

function currentContext(includeSelection) {
  const editor = vscode.window.activeTextEditor;
  if (!editor) throw new Error("В VS Code нет активного текстового редактора");
  const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
  const relativePath = folder
    ? path.relative(folder.uri.fsPath, editor.document.uri.fsPath)
    : editor.document.uri.toString();
  const text = editor.document.getText();
  const selection = includeSelection && !editor.selection.isEmpty
    ? editor.document.getText(editor.selection).slice(0, MAX_CONTEXT)
    : "";
  return {
    path: relativePath,
    language: editor.document.languageId,
    text: text.slice(0, MAX_CONTEXT),
    truncated: text.length > MAX_CONTEXT,
    selection,
    selection_truncated: includeSelection && !editor.selection.isEmpty
      && editor.document.getText(editor.selection).length > MAX_CONTEXT
  };
}

function problems() {
  const items = [];
  let total = 0;
  for (const [uri, diagnostics] of vscode.languages.getDiagnostics()) {
    for (const item of diagnostics) {
      total++;
      if (items.length >= MAX_PROBLEMS) continue;
      const severity = ["Error", "Warning", "Information", "Hint"][item.severity] || "Unknown";
      items.push({
        path: vscode.workspace.asRelativePath(uri, false),
        line: item.range.start.line + 1,
        column: item.range.start.character + 1,
        severity,
        message: item.message.slice(0, 2_000),
        source: item.source || "",
        code: item.code?.value ?? item.code ?? ""
      });
    }
  }
  return { problems: items, truncated: total > MAX_PROBLEMS };
}

function formatToolResult(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function confirmationMessage(tool, input) {
  const message = new vscode.MarkdownString();
  if (input.path) {
    message.appendText("File: ");
    message.appendCode(input.path);
  } else if (input.command) {
    message.appendText("Command:\n");
    message.appendCodeblock(input.command, "sh");
  }
  return {
    title: tool.confirmation,
    message: message.value ? message : new vscode.MarkdownString(tool.confirmation)
  };
}

class VSCodeChatTool {
  constructor(tool) {
    this.tool = tool;
  }

  async prepareInvocation(options) {
    const input = options.input || {};
    return {
      invocationMessage: this.tool.label,
      ...(this.tool.confirmation
        ? { confirmationMessages: confirmationMessage(this.tool, input) }
        : {})
    };
  }

  async invoke(options, token) {
    if (token.isCancellationRequested) throw new Error("Request cancelled");
    const input = options.input || {};
    if (typeof input.content === "string" && Buffer.byteLength(input.content, "utf8") > MAX_BODY) {
      throw new Error(`File content exceeds the ${MAX_BODY} byte limit`);
    }
    const result = await dispatch(this.tool.endpoint, input);
    return new vscode.LanguageModelToolResult([
      new vscode.LanguageModelTextPart(formatToolResult(result))
    ]);
  }
}

function registerChatTools(context) {
  if (typeof vscode.lm?.registerTool !== "function") {
    vscode.window.showWarningMessage(
      "LM Agent Chat tools require VS Code 1.100 or newer with Copilot Chat enabled."
    );
    return;
  }
  for (const tool of chatTools) {
    context.subscriptions.push(vscode.lm.registerTool(tool.name, new VSCodeChatTool(tool)));
  }
}

async function installChatProfile() {
  const source = path.join(extensionContext.extensionPath, "agent-profile", "lm-agent.agent.md");
  const content = fs.readFileSync(source, "utf8");
  const copilotHome = path.resolve(process.env.COPILOT_HOME || path.join(os.homedir(), ".copilot"));
  const target = path.join(copilotHome, "agents", "lm-agent.agent.md");
  fs.mkdirSync(path.dirname(target), { recursive: true });

  if (fs.existsSync(target)) {
    if (fs.readFileSync(target, "utf8") === content) {
      vscode.window.showInformationMessage("Профиль LM Agent уже установлен для Copilot.");
      return;
    }
    const choice = await vscode.window.showWarningMessage(
      `Профиль ${target} уже существует. Заменить его?`,
      { modal: true },
      "Заменить"
    );
    if (choice !== "Заменить") return;
  }

  fs.writeFileSync(target, content, { encoding: "utf8", mode: 0o600 });
  vscode.window.showInformationMessage(
    "Профиль LM Agent установлен. Выберите его в списке агентов VS Code Chat."
  );
}

async function dispatch(endpoint, data) {
  if (endpoint === "health") {
    return {
      status: "ok",
      project_folder: projectRoot || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || "",
      analysis_folder: analysisRoot || ""
    };
  }
  if (endpoint === "select_project") return selectFolder("project");
  if (endpoint === "select_analysis") return selectFolder("analysis");
  if (endpoint === "list") return listFiles(data.path || ".", data.scope || "project");
  if (endpoint === "read") return readFile(data.path, data.scope || "project");
  if (endpoint === "create") return createFile(data.path, data.content);
  if (endpoint === "context") return currentContext(data.include_selection !== false);
  if (endpoint === "problems") return problems();
  if (endpoint === "open") {
    const { root, target, uri } = resolveFolderPath(data.path, "project");
    const line = Math.max(1, Number.isInteger(data.line) ? data.line : 1);
    const column = Math.max(1, Number.isInteger(data.column) ? data.column : 1);
    const document = await vscode.workspace.openTextDocument(uri);
    const position = document.validatePosition(
      new vscode.Position(Math.min(line - 1, document.lineCount - 1), column - 1)
    );
    const editor = await vscode.window.showTextDocument(document);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenter);
    return { path: path.relative(root, target), line: position.line + 1, column: position.character + 1 };
  }
  if (endpoint === "edit") {
    if (typeof data.content !== "string") throw new Error("Поле content должно быть строкой");
    const { root, target, uri } = resolveFolderPath(data.path, "project");
    await vscode.workspace.fs.stat(uri);
    const document = await vscode.workspace.openTextDocument(uri);
    const fullRange = new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length));
    const edit = new vscode.WorkspaceEdit();
    edit.replace(uri, fullRange, data.content);
    if (!await vscode.workspace.applyEdit(edit)) throw new Error("VS Code не применил изменение");
    if (!await document.save()) throw new Error("VS Code не смог сохранить файл");
    return { path: path.relative(root, target) };
  }
  if (endpoint === "terminal") {
    if (typeof data.command !== "string" || !data.command.trim()) throw new Error("Команда не задана");
    const cwd = fs.realpathSync(rootForScope("project"));
    let terminal = agentTerminals.get(cwd);
    if (!terminal || !vscode.window.terminals.includes(terminal)) {
      terminal = vscode.window.createTerminal({ name: "LM Agent", cwd: vscode.Uri.file(cwd) });
      agentTerminals.set(cwd, terminal);
    }
    terminal.show();
    terminal.sendText(data.command);
    return { message: `Команда отправлена в терминал VS Code (cwd: ${cwd}); вывод и статус выполнения не проверялись.` };
  }
  throw new Error("Неизвестный endpoint");
}

function matchesToken(request) {
  let configured;
  try {
    configured = fs.readFileSync(tokenFilePath(), "utf8").trim();
  } catch {
    return false;
  }
  const supplied = (request.headers.authorization || "").replace(/^Bearer\s+/i, "");
  const a = Buffer.from(configured);
  const b = Buffer.from(supplied);
  return a.length > 0 && a.length === b.length && crypto.timingSafeEqual(a, b);
}

function activate(context) {
  extensionContext = context;
  projectRoot = validSavedFolder(context.globalState.get("projectRoot"));
  analysisRoot = validSavedFolder(context.globalState.get("analysisRoot"));
  const port = vscode.workspace.getConfiguration("lmAgent").get("bridgePort", 8765);
  const server = http.createServer(async (request, response) => {
    if (!isLoopback(request.socket.remoteAddress)) return send(response, 403, { error: "Только локальные подключения разрешены" });
    const host = (request.headers.host || "").split(":")[0].replace(/^\[|\]$/g, "");
    if (!["127.0.0.1", "localhost"].includes(host)) return send(response, 403, { error: "Недопустимый Host" });
    if (!matchesToken(request)) return send(response, 401, { error: "Неверный токен моста VS Code" });
    const endpoint = request.url?.split("?")[0].replace(/^\/+/, "");
    if (request.method === "GET" && endpoint === "health") {
      return send(response, 200, await dispatch("health", {}));
    }
    if (request.method !== "POST") return send(response, 404, { error: "Не найдено" });
    try {
      const data = await readBody(request);
      send(response, 200, await dispatch(endpoint, data));
    } catch (error) {
      send(response, 400, { error: error.message || String(error) });
    }
  });
  server.on("error", error => {
    vscode.window.showErrorMessage(`LM Agent: не удалось запустить локальный мост VS Code: ${error.message}`);
  });
  server.listen(port, "127.0.0.1");
  context.subscriptions.push({ dispose: () => server.close() });
  registerChatTools(context);
  context.subscriptions.push(vscode.window.onDidCloseTerminal(terminal => {
    for (const [cwd, item] of agentTerminals) {
      if (item === terminal) agentTerminals.delete(cwd);
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("lmAgent.showBridgeStatus", async () => {
    const tokenPath = tokenFilePath();
    let tokenState = "не создан (запустите LM Agent)";
    try {
      fs.accessSync(tokenPath, fs.constants.R_OK);
      tokenState = "готов";
    } catch {}
    const project = projectRoot || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || "не выбрана";
    const analysis = analysisRoot || "не выбрана";
    vscode.window.showInformationMessage(`LM Agent bridge: 127.0.0.1:${port}; проект: ${project}; анализ: ${analysis}; токен: ${tokenState}`);
  }));
  context.subscriptions.push(vscode.commands.registerCommand("lmAgent.installChatProfile", installChatProfile));
}

function validSavedFolder(folder) {
  if (!folder) return undefined;
  try {
    const resolved = fs.realpathSync(folder);
    return fs.statSync(resolved).isDirectory() ? resolved : undefined;
  } catch {
    return undefined;
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
