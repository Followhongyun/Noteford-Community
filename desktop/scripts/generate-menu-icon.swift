// Vector source for the macOS menu bar template: 18 pt, with a 2x Retina asset.
import AppKit
import Foundation
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
for scale in [1, 2] {
    let size = 18 * scale
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let context = NSGraphicsContext(bitmapImageRep: bitmap)!
    NSGraphicsContext.current = context
    context.shouldAntialias = true
    let transform = AffineTransform(scale: CGFloat(scale))
    // Closed notebook cover, spine and two short lines on the front cover.
    let path = NSBezierPath(roundedRect: NSRect(x: 3.5, y: 2, width: 11.5, height: 14),
                            xRadius: 1.6, yRadius: 1.6)
    path.move(to: NSPoint(x: 6.5, y: 2))
    path.line(to: NSPoint(x: 6.5, y: 16))
    path.move(to: NSPoint(x: 9, y: 11.5))
    path.line(to: NSPoint(x: 12.5, y: 11.5))
    path.move(to: NSPoint(x: 9, y: 8.5))
    path.line(to: NSPoint(x: 12.5, y: 8.5))
    path.transform(using: transform)
    path.lineWidth = 1.4 * CGFloat(scale)
    path.lineJoinStyle = .round
    path.lineCapStyle = .round
    NSColor.black.setStroke()
    path.stroke()
    NSGraphicsContext.restoreGraphicsState()
    let name = scale == 1 ? "notefordTemplate.png" : "notefordTemplate@2x.png"
    try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name))
}
