// Draws the app icon: a macOS squircle with waveform bars in two speaker colours.
import AppKit

let S: CGFloat = 1024
let img = NSImage(size: NSSize(width: S, height: S))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

// Apple's icon grid: 824pt rounded rect centred in 1024, radius ~185
let inset: CGFloat = 100, r: CGFloat = 185
let rect = CGRect(x: inset, y: inset, width: S - 2 * inset, height: S - 2 * inset)
let shape = NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r)

ctx.saveGState()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
shadow.shadowBlurRadius = 24
shadow.shadowOffset = NSSize(width: 0, height: -10)
shadow.set()
NSColor.black.setFill()
shape.fill()
ctx.restoreGState()

shape.addClip()
NSGradient(colors: [NSColor(red: 0.16, green: 0.12, blue: 0.30, alpha: 1),
                    NSColor(red: 0.07, green: 0.06, blue: 0.14, alpha: 1)])!.draw(in: rect, angle: -90)
// soft glow behind the bars
NSGradient(colors: [NSColor(red: 0.55, green: 0.45, blue: 1, alpha: 0.28), .clear])!
    .draw(fromCenter: CGPoint(x: S / 2, y: S / 2), radius: 0, toCenter: CGPoint(x: S / 2, y: S / 2), radius: 420, options: [])

let heights: [CGFloat] = [0.16, 0.34, 0.56, 0.40, 0.24, 0.30, 0.62, 0.46, 0.70, 0.36, 0.18]
let speakerA = NSColor(red: 1.00, green: 0.55, blue: 0.48, alpha: 1)   // coral
let speakerB = NSColor(red: 0.45, green: 0.90, blue: 0.80, alpha: 1)   // mint
let w: CGFloat = 40, gap: CGFloat = 22
let total = CGFloat(heights.count) * w + CGFloat(heights.count - 1) * gap
var x = (S - total) / 2
for (i, h) in heights.enumerated() {
    let hh = h * 620
    let bar = NSBezierPath(roundedRect: CGRect(x: x, y: S / 2 - hh / 2, width: w, height: hh), xRadius: w / 2, yRadius: w / 2)
    (i < 5 ? speakerA : speakerB).setFill()   // first voice, then the second voice answers
    bar.fill()
    x += w + gap
}
img.unlockFocus()

let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
