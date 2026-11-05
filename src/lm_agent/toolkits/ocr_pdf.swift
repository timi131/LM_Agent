// Распознавание текста в PDF без текстового слоя (сканы, «печать в PDF» картинками)
// встроенным движком macOS (Vision): русский и английский, без интернета.
// Использование: ocr_pdf файл.pdf результат.txt — текст страниц, разделённых пустой строкой.
// В файл, а не в stdout: Vision пишет в stdout собственные служебные сообщения.
import Foundation
import PDFKit
import Vision

guard CommandLine.arguments.count > 2, let doc = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else {
    FileHandle.standardError.write("не удалось открыть PDF\n".data(using: .utf8)!)
    exit(1)
}

var pages: [String] = []
for index in 0..<doc.pageCount {
    guard let page = doc.page(at: index) else { continue }
    let bounds = page.bounds(for: .mediaBox)
    let scale: CGFloat = 3  // ~216 dpi: мелкий шрифт распознаётся заметно лучше
    let size = CGSize(width: bounds.width * scale, height: bounds.height * scale)
    let image = page.thumbnail(of: size, for: .mediaBox)
    guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ru-RU", "en-US"]
    request.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    pages.append(lines.joined(separator: "\n"))
}
try pages.joined(separator: "\n\n").write(toFile: CommandLine.arguments[2], atomically: true, encoding: .utf8)
