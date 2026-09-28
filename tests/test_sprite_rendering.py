import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SpriteRenderingTests(unittest.TestCase):
    def test_sprite_cache_preserves_pixels_and_avoids_repeated_decode(self):
        fixture = Path.home() / ".hermes/pets/nukey/spritesheet.webp"
        if not fixture.exists():
            self.skipTest("Install the Nukey pet to run the real WebP rendering benchmark")
        source = (ROOT / "menubar/ProviderQuotaMenuBar.swift").read_text()
        marker = "    static func decodedSprite(at url: URL) -> NSImage? {"
        if marker in source:
            method = source[source.index(marker):source.index("    private func loadArt()", source.index(marker))]
        else:
            method = "static func decodedSprite(at url: URL) -> NSImage? { NSImage(contentsOf: url) }"
        program = "import AppKit\nenum Harness {\n" + method + "}\n" + r'''
let url = URL(fileURLWithPath: CommandLine.arguments[1])
func render(_ image: NSImage) -> (Double, Data) {
    let start = ProcessInfo.processInfo.systemUptime
    var pixels = Data()
    for i in 0..<160 {
        autoreleasepool {
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 96, pixelsHigh: 104, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            let context = NSGraphicsContext(bitmapImageRep: rep)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = context
            image.draw(in: NSRect(x: 0, y: 0, width: 96, height: 104), from: NSRect(x: (i % 8) * 192, y: 0, width: 192, height: 208), operation: .copy, fraction: 1)
            context.flushGraphics()
            NSGraphicsContext.restoreGraphicsState()
            if i < 8 { pixels.append(rep.bitmapData!, count: rep.bytesPerRow * rep.pixelsHigh) }
        }
    }
    return (ProcessInfo.processInfo.systemUptime - start, pixels)
}
let original = NSImage(contentsOf: url)!
let decoded = Harness.decodedSprite(at: url)!
let baseline = render(original)
let optimized = render(decoded)
guard original.size == decoded.size else { fatalError("Sprite dimensions changed") }
guard baseline.1.count == optimized.1.count else { fatalError("Sprite pixel size changed") }
let difference = zip(baseline.1, optimized.1).map { abs(Int($0) - Int($1)) }.max()!
guard difference <= 2 else { fatalError("Sprite pixels changed: \(difference)") }
print("baseline_seconds=\(baseline.0) cached_seconds=\(optimized.0) max_pixel_difference=\(difference)")
guard optimized.0 < baseline.0 * 0.65 else { fatalError("Sprite cache still repeats expensive decoding") }
'''
        with tempfile.TemporaryDirectory() as tmp:
            swift = Path(tmp) / "sprite.swift"
            binary = Path(tmp) / "sprite"
            swift.write_text(program)
            subprocess.run(["xcrun", "swiftc", "-O", str(swift), "-o", str(binary)], check=True, capture_output=True, timeout=60)
            result = subprocess.run([str(binary), str(fixture)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
