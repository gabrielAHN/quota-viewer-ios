import AppKit
import Foundation
import SQLite3

struct QuotaPayload: Decodable {
    let generatedAt: String
    let providers: [QuotaProvider]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case providers
    }
}

struct QuotaProvider: Codable {
    let provider: String
    let label: String
    let status: String
    let plan: String?
    let windows: [QuotaWindow]
    let details: [String]
    let message: String?
}

struct QuotaWindow: Codable {
    // "Limit reached" = the window is genuinely OUT (you can't use it) → red /
    // exhausted. A low-but-usable window (e.g. 4% weekly left) is NOT "reached" — it
    // still has quota; it reads as low (orange, ≤15%) instead. Previously this was 5%,
    // which flagged a still-usable 4% as "no quota".
    private static let limitReachedRemainingPercent = 0.5

    let label: String
    let remainingPercent: Double?
    let remainingAmount: Double?
    let currency: String?
    let resetsAt: String?
    let detail: String?
    let warning: Bool

    var limitReached: Bool {
        Self.reachesLimit(remainingPercent: remainingPercent, remainingAmount: remainingAmount)
    }

    static func reachesLimit(remainingPercent: Double?, remainingAmount: Double?) -> Bool {
        (remainingPercent.map { $0 <= limitReachedRemainingPercent } ?? false)
            || (remainingAmount.map { $0 <= 0 } ?? false)
    }

    enum CodingKeys: String, CodingKey {
        case label
        case remainingPercent = "remaining_percent"
        case remainingAmount = "remaining_amount"
        case currency
        case resetsAt = "resets_at"
        case detail
        case warning
    }
}

struct DesktopStatus {
    let running: Bool
    let mode: String
    let authMode: String
    let remoteURL: String?
    let signedIn: Bool
    let reachable: Bool
    let profile: String?
    let provider: String?
    let model: String?

    var connected: Bool {
        signedIn && reachable
    }
}

struct MenuSnapshot {
    let payload: QuotaPayload
    let desktop: DesktopStatus
}

struct ProviderActivityInstance: Decodable, Equatable {
    let completedAt: Double?
    let key: String
    let model: String
    let profile: String
    let provider: String
    let providerColor: String
    let providerLabel: String
    let sessionId: String
    let startedAt: Double
    let status: String?
    let title: String

    var completed: Bool {
        status == "completed"
    }

    var sleeping: Bool {
        status == "sleeping"
    }

    var failed: Bool {
        status == "error" || status == "failed"
    }

    var activityRank: Int {
        switch status {
        case "error", "failed", "waiting", "working": return 0
        case "completed": return 1
        default: return 2
        }
    }
}

extension NSColor {
    convenience init?(activityHex value: String) {
        let hex = value.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        guard hex.count == 6, let number = Int(hex, radix: 16) else { return nil }
        self.init(
            deviceRed: CGFloat((number >> 16) & 0xff) / 255,
            green: CGFloat((number >> 8) & 0xff) / 255,
            blue: CGFloat(number & 0xff) / 255,
            alpha: 1
        )
    }

    // Hermes brand palette (mirrors the Desktop app's --ui-* / --theme-* CSS
    // variables) so this menu reads as part of Hermes. Blue is the primary accent
    // — matching the Desktop theme (--theme-primary #0053fd); provider brand
    // colours are used only as small secondary identity tints.
    static let hermesBlue = NSColor(activityHex: "0053fd")!        // --theme-primary / --ui-blue (primary accent)
    static let hermesGreen = NSColor(activityHex: "1f8a65")!       // --ui-green (connected / healthy)
    static let hermesOrange = NSColor(activityHex: "db704b")!      // --ui-orange (low / waiting)
    static let hermesRed = NSColor(activityHex: "cf2d56")!         // --ui-red (disconnected / error)
    static let hermesYellow = NSColor(activityHex: "c08532")!      // --ui-yellow (warning)
}

final class HoverGlowButton: NSButton {
    var glowColor: NSColor = .controlAccentColor {
        didSet {
            updateGlow()
        }
    }
    private var hoverArea: NSTrackingArea?
    private var hovering = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configure()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        configure()
    }

    private func configure() {
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 7
        layer?.masksToBounds = false
        updateGlow()
    }

    override func updateTrackingAreas() {
        if let hoverArea {
            removeTrackingArea(hoverArea)
        }
        let area = NSTrackingArea(rect: bounds, options: [.activeAlways, .inVisibleRect, .mouseEnteredAndExited], owner: self)
        addTrackingArea(area)
        hoverArea = area
        super.updateTrackingAreas()
    }

    override func layout() {
        super.layout()
        layer?.shadowPath = CGPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), cornerWidth: 7, cornerHeight: 7, transform: nil)
    }

    override func mouseEntered(with event: NSEvent) {
        hovering = true
        updateGlow()
    }

    override func mouseExited(with event: NSEvent) {
        hovering = false
        updateGlow()
    }

    private func updateGlow() {
        // Always-on glow (colored fill + border + soft shadow) that intensifies
        // on hover, so the buttons read as glowing Hermes chips at rest.
        layer?.backgroundColor = glowColor.withAlphaComponent(hovering ? 0.30 : 0.16).cgColor
        layer?.borderColor = glowColor.withAlphaComponent(hovering ? 1.0 : 0.6).cgColor
        layer?.borderWidth = 1
        layer?.shadowColor = glowColor.cgColor
        layer?.shadowOpacity = hovering ? 0.95 : 0.55
        layer?.shadowRadius = hovering ? 10 : 6
        layer?.shadowOffset = .zero
    }
}

// A transparent menu-row container that, the moment it lands in the menu's window,
// forces that window's appearance to the app's chosen theme. That re-themes the
// NATIVE menu vibrancy (which otherwise always renders in the system appearance),
// so the dropdown keeps its translucent effect in BOTH Light and Dark — and the
// semantic text/label colours resolve for the theme automatically. nil = follow
// the system.
final class ThemedMenuContainer: NSView {
    var forcedAppearance: NSAppearance?
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        window?.appearance = forcedAppearance
    }
}

final class HoverGlowSwitch: NSSwitch {
    private var hoverArea: NSTrackingArea?
    private var hovering = false
    var glowEnabled = false {
        didSet {
            updateGlow()
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configure()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        configure()
    }

    private func configure() {
        wantsLayer = true
        layer?.cornerRadius = 11
        layer?.masksToBounds = false
        updateGlow()
    }

    override func updateTrackingAreas() {
        if let hoverArea {
            removeTrackingArea(hoverArea)
        }
        let area = NSTrackingArea(rect: bounds, options: [.activeAlways, .inVisibleRect, .mouseEnteredAndExited], owner: self)
        addTrackingArea(area)
        hoverArea = area
        super.updateTrackingAreas()
    }

    override func layout() {
        super.layout()
        layer?.shadowPath = CGPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), cornerWidth: 11, cornerHeight: 11, transform: nil)
    }

    override func mouseEntered(with event: NSEvent) {
        hovering = true
        updateGlow()
    }

    override func mouseExited(with event: NSEvent) {
        hovering = false
        updateGlow()
    }

    private func updateGlow() {
        let color = glowEnabled ? NSColor.systemPurple : NSColor.secondaryLabelColor
        layer?.backgroundColor = color.withAlphaComponent(glowEnabled ? (hovering ? 0.22 : 0.11) : (hovering ? 0.055 : 0)).cgColor
        layer?.borderColor = color.withAlphaComponent(glowEnabled ? (hovering ? 0.82 : 0.48) : (hovering ? 0.2 : 0.08)).cgColor
        layer?.borderWidth = 1
        layer?.shadowColor = color.cgColor
        layer?.shadowOpacity = glowEnabled ? (hovering ? 0.9 : 0.48) : (hovering ? 0.12 : 0)
        layer?.shadowRadius = glowEnabled ? (hovering ? 10 : 6) : (hovering ? 3 : 0)
        layer?.shadowOffset = .zero
    }
}

final class TransparentMenuEffectView: NSVisualEffectView {
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        window?.isOpaque = false
        window?.backgroundColor = .clear
        window?.alphaValue = 0.94
        window?.contentView?.wantsLayer = true
        window?.contentView?.layer?.backgroundColor = NSColor.clear.cgColor
    }
}

// A pet as Hermes ships it: discovered from ~/.hermes/pets/<id>/pet.json so the
// menu-bar adopts whatever pets Hermes has installed (not a hardcoded sprite).
struct PetInfo: Equatable {
    let id: String
    let displayName: String
    let spritesheetURL: URL
    let thumbnailURL: URL?
}

// One drawn companion: a synthesized provider "activity" plus the pet art chosen
// for that provider. Each tile carries its own pet, so every provider can have a
// different companion shown at once.
// One running session's mark in the pet UI: a provider-coloured dot. It's shown
// only while the session is running and removed the moment it finishes.
struct SessionMark: Equatable {
    let hex: String     // provider colour
    let busy: Bool      // actively processing (bright pulse) vs idle (dim)
    // The gateway flagged this session as WAITING FOR YOU. `needsInput` = a plain
    // input prompt; `needsPermission` = a permission / approval prompt. Either draws
    // an attention badge on the point and makes the pet WAVE — never the error
    // animation, since it isn't an error. Permission takes visual priority.
    var needsInput: Bool = false
    var needsPermission: Bool = false
}

struct PetTile: Equatable {
    let instance: ProviderActivityInstance
    let pet: PetInfo?
    // How many running sessions this one pet stands in for (count badge when many).
    var sessionCount: Int = 1
    // One mark per session — a graphic for every session, in its provider colour,
    // plus a check for any that just completed.
    var sessions: [SessionMark] = []
    // Which source this pet belongs to (GatewayKind rawValue), so a right-click
    // "Turn Off" can disable the right source's pet. Empty for placeholders.
    var sourceKey: String = ""
}

enum PetCatalog {
    static var petsDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".hermes/pets")
    }
    // Pets downloaded via a REMOTE Hermes Desktop land on the gateway, not here;
    // the app syncs them into this cache so they show up locally.
    static var cacheDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".hermes/pets-cache")
    }

    // Pets hidden from the picker: the Lulu capybaras and cats. Nukey (Hermes's
    // default) and the petdex pets (Boba, Capy, Scoop, …) are all offered.
    static func isHidden(_ id: String) -> Bool {
        let lower = id.lowercased()
        return lower.hasPrefix("lulu") || lower.hasSuffix("cat")
    }

    static func installed() -> [PetInfo] {
        let fm = FileManager.default
        var pets: [PetInfo] = []
        var seen = Set<String>()
        // Local pets dir wins over the gateway cache (scanned first).
        for base in [petsDir, cacheDir] {
            guard let entries = try? fm.contentsOfDirectory(
                at: base, includingPropertiesForKeys: [.isDirectoryKey]
            ) else { continue }
            for dir in entries {
                let name = dir.lastPathComponent
                if name.hasPrefix(".") { continue }
                let isDir = (try? dir.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory ?? false
                guard isDir else { continue }
                guard let data = try? Data(contentsOf: dir.appendingPathComponent("pet.json")),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else { continue }
                let id = (obj["id"] as? String) ?? name
                if seen.contains(id) || isHidden(id) { continue }
                let sheet = dir.appendingPathComponent((obj["spritesheetPath"] as? String) ?? "spritesheet.webp")
                guard fm.fileExists(atPath: sheet.path) else { continue }
                seen.insert(id)
                let display = (obj["displayName"] as? String) ?? id.capitalized
                // Thumbnails: local catalog first, then a cached copy.
                let localThumb = petsDir.appendingPathComponent(".thumbs/\(id).png")
                let cacheThumb = dir.appendingPathComponent("thumbnail.png")
                let thumb = fm.fileExists(atPath: localThumb.path) ? localThumb
                    : (fm.fileExists(atPath: cacheThumb.path) ? cacheThumb : nil)
                pets.append(PetInfo(id: id, displayName: display, spritesheetURL: sheet, thumbnailURL: thumb))
            }
        }
        return pets.sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }
    }

    // The active pet: the user's saved choice if still installed, else the first
    // installed pet (Hermes ships "nukey" by default).
    static func selected() -> PetInfo? {
        let all = installed()
        if let saved = UserDefaults.standard.string(forKey: "selectedPet"),
           let match = all.first(where: { $0.id == saved }) {
            return match
        }
        return all.first
    }

    // The pet chosen for a given key (a source: "hermes" / "local"). Falls back
    // to the global selection when that key has no explicit choice yet, so a
    // fresh install still shows a companion for every source.
    static func selected(forKey key: String) -> PetInfo? {
        let all = installed()
        if let saved = UserDefaults.standard.string(forKey: "selectedPet.\(key)"),
           let match = all.first(where: { $0.id == saved }) {
            return match
        }
        return selected()
    }

    static func setSelected(_ id: String, forKey key: String) {
        UserDefaults.standard.set(id, forKey: "selectedPet.\(key)")
    }

    // A small still image of a pet — its thumbnail if one exists, otherwise the
    // idle frame cropped from its spritesheet — so the UI can always show the
    // ACTUAL pet (most installed pets ship only a spritesheet, no thumbnail).
    static func petImage(_ pet: PetInfo, size: CGFloat = 20) -> NSImage? {
        if let url = pet.thumbnailURL, let img = NSImage(contentsOf: url) {
            img.size = NSSize(width: size, height: size)
            return img
        }
        guard let sheet = NSImage(contentsOf: pet.spritesheetURL) else { return nil }
        let frameW: CGFloat = 192, frameH: CGFloat = 208   // idle frame = row 0, col 0
        let source = NSRect(x: 0, y: sheet.size.height - frameH, width: frameW, height: frameH)
        let out = NSImage(size: NSSize(width: size, height: size))
        out.lockFocus()
        NSGraphicsContext.current?.imageInterpolation = .none
        let scale = min(size / frameW, size / frameH)
        let w = frameW * scale, h = frameH * scale
        sheet.draw(in: NSRect(x: (size - w) / 2, y: (size - h) / 2, width: w, height: h),
                   from: source, operation: .sourceOver, fraction: 1)
        out.unlockFocus()
        return out
    }

    // Whether a key (source) shows a pet at all (defaults on). Lets you activate
    // a pet per source from the menu.
    static func petEnabled(forKey key: String) -> Bool {
        UserDefaults.standard.object(forKey: "petEnabled.\(key)") == nil
            || UserDefaults.standard.bool(forKey: "petEnabled.\(key)")
    }

    static func setPetEnabled(_ on: Bool, forKey key: String) {
        UserDefaults.standard.set(on, forKey: "petEnabled.\(key)")
    }
}

final class ActivityPetsView: NSView {
    static let tileWidth: CGFloat = 56
    static let tileHeight: CGFloat = 74   // extra room at the bottom for a session-dots row
    static let tileGap: CGFloat = 5
    private var phase = 0
    private var animationTimer: Timer?
    private var dragDistance: CGFloat = 0
    private var lastDragLocation: NSPoint?
    private var toolTipTags: [NSView.ToolTipTag] = []
    // Hermes sprite frames are a fixed 192×208 (agent/pet/constants FRAME_W/H);
    // the column/row counts are derived from each sheet so any atlas shape fits
    // (8×9 Codex, 9×8 legacy, or the 8×11 sheets that add a sleep row 10).
    static let frameW: CGFloat = 192
    static let frameH: CGFloat = 208
    // One pet per tile. Sprite / thumbnail images are cached by pet id so the
    // per-frame redraw (every 0.16s) never re-reads them from disk.
    private var sheetCache: [String: NSImage] = [:]
    private var thumbCache: [String: NSImage] = [:]
    // How many frames each row actually uses, per pet. Sheets are left-packed and
    // rows vary in length (e.g. the "duck" packs 4 frames where Nukey packs 8), so
    // we loop each row's real frame count instead of a fixed guess — otherwise a
    // short row runs into blank cells and the pet flickers/vanishes. Derived once
    // per sheet by scanning alpha (see detectFramesPerRow) and cached by pet id.
    private var frameCounts: [String: [Int]] = [:]
    var onMove: ((CGFloat, CGFloat) -> Void)?
    var onSelect: ((ProviderActivityInstance) -> Void)?
    // Right-click actions: turn a source's pet off, or resize all the pets.
    var onTurnOff: ((PetTile) -> Void)?
    var onScaleChanged: (() -> Void)?
    // A user-chosen magnification for the whole pet strip (right-click → Pet Size).
    // Drawing stays in logical tile coordinates; the view just scales up, and
    // hit-testing / tooltips divide back down. Three fixed sizes.
    static let petSizes: [(name: String, value: CGFloat)] = [
        ("Small", 1.0), ("Medium", 1.5), ("Large", 2.0)
    ]
    var scale: CGFloat = {
        let saved = CGFloat(UserDefaults.standard.double(forKey: "petScale"))
        // Snap to the nearest known size (default Small) so a stale value is safe.
        return petSizes.min { abs($0.value - saved) < abs($1.value - saved) }
            .map { abs($0.value - saved) < 0.25 ? $0.value : 1 } ?? 1
    }()
    // The tile the context menu was opened over (so its menu items act on it).
    private var contextTile: PetTile?
    var tiles: [PetTile] = [] {
        didSet {
            loadArt()
            rebuildToolTips()
            needsDisplay = true
        }
    }

    private func loadArt() {
        for tile in tiles {
            guard let pet = tile.pet else { continue }
            if sheetCache[pet.id] == nil, let img = NSImage(contentsOf: pet.spritesheetURL) {
                sheetCache[pet.id] = img
                frameCounts[pet.id] = Self.detectFramesPerRow(img)
            }
            if thumbCache[pet.id] == nil, let url = pet.thumbnailURL, let img = NSImage(contentsOf: url) {
                thumbCache[pet.id] = img
            }
        }
    }

    // Count the left-packed frames in each row of a sprite sheet by scanning the
    // alpha channel: a row's frame count is how many leading columns hold visible
    // pixels. Cheap (sampled, once per sheet) and lets any atlas shape animate
    // without a hard-coded frames-per-row. Returns [] if the bitmap is unreadable,
    // in which case drawNukey falls back to a sensible default.
    private static func detectFramesPerRow(_ image: NSImage) -> [Int] {
        guard let rep = NSBitmapImageRep(data: image.tiffRepresentation ?? Data()),
              let data = rep.bitmapData else { return [] }
        let fw = Int(frameW), fh = Int(frameH)
        let pw = rep.pixelsWide, ph = rep.pixelsHigh
        guard fw > 0, fh > 0, pw >= fw, ph >= fh else { return [] }
        let cols = pw / fw, rows = ph / fh
        let bpr = rep.bytesPerRow, bpp = rep.bitsPerPixel / 8, spp = rep.samplesPerPixel
        let hasAlpha = rep.hasAlpha && spp >= 4
        let alphaOffset = rep.bitmapFormat.contains(.alphaFirst) ? 0 : spp - 1
        // No alpha channel ⇒ can't tell blanks apart; treat every column as used.
        guard hasAlpha else { return Array(repeating: cols, count: rows) }
        func occupied(colX: Int, rowY: Int) -> Bool {
            var y = rowY
            while y < rowY + fh {
                var x = colX
                while x < colX + fw {
                    if Int(data[y * bpr + x * bpp + alphaOffset]) > 16 { return true }
                    x += 4   // sparse sample — plenty to spot a non-empty frame
                }
                y += 4
            }
            return false
        }
        var out: [Int] = []
        out.reserveCapacity(rows)
        for r in 0..<rows {
            var count = 0
            for c in 0..<cols {
                if occupied(colX: c * fw, rowY: r * fh) { count += 1 } else { break }
            }
            out.append(max(count, 1))
        }
        return out
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        // 10 fps redraw drives the smooth bob/pulse; the sprite frame advances
        // every 2 ticks (→ a calm 5 fps, see drawNukey), so each frame is held for
        // exactly the same time — even, not staggered. The 1680 wrap is a multiple
        // of every frame count (1…8) and of the sin periods, so nothing jumps when
        // the counter rolls over.
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            phase = (phase + 1) % 1680
            needsDisplay = true
        }
        // Run in .common modes so the pet keeps animating while the status-bar
        // menu is open (menu tracking switches the run loop to event-tracking
        // mode, where a default-mode timer would be paused).
        RunLoop.current.add(timer, forMode: .common)
        animationTimer = timer
    }

    required init?(coder: NSCoder) {
        nil
    }

    deinit {
        animationTimer?.invalidate()
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .openHand)
    }

    override func mouseDown(with event: NSEvent) {
        dragDistance = 0
        lastDragLocation = NSEvent.mouseLocation
    }

    override func mouseDragged(with event: NSEvent) {
        let current = NSEvent.mouseLocation
        guard let previous = lastDragLocation else { return }
        let deltaX = current.x - previous.x
        let deltaY = current.y - previous.y
        dragDistance += hypot(deltaX, deltaY)
        lastDragLocation = current
        onMove?(deltaX, deltaY)
    }

    override func mouseUp(with event: NSEvent) {
        lastDragLocation = nil
        guard dragDistance < 4, let instance = instance(at: convert(event.locationInWindow, from: nil)) else { return }
        onSelect?(instance)
    }

    // Right-click: a small menu to resize the pets (three sizes) or turn one off.
    override func rightMouseDown(with event: NSEvent) {
        contextTile = tile(at: convert(event.locationInWindow, from: nil))
        let menu = NSMenu()

        let sizeMenu = NSMenu()
        for (index, option) in Self.petSizes.enumerated() {
            let item = NSMenuItem(title: option.name, action: #selector(setSize(_:)), keyEquivalent: "")
            item.target = self
            item.tag = index
            item.state = abs(scale - option.value) < 0.01 ? .on : .off
            sizeMenu.addItem(item)
        }
        let sizeItem = NSMenuItem(title: "Pet Size", action: nil, keyEquivalent: "")
        sizeItem.submenu = sizeMenu
        menu.addItem(sizeItem)

        if let tile = contextTile, !tile.sourceKey.isEmpty {
            menu.addItem(.separator())
            let off = NSMenuItem(title: "Turn Off \(tile.instance.profile) Pet",
                                 action: #selector(turnOffPet), keyEquivalent: "")
            off.target = self
            menu.addItem(off)
        }
        NSMenu.popUpContextMenu(menu, with: event, for: self)
    }

    @objc private func setSize(_ sender: NSMenuItem) {
        guard Self.petSizes.indices.contains(sender.tag) else { return }
        scale = Self.petSizes[sender.tag].value
        UserDefaults.standard.set(Double(scale), forKey: "petScale")
        rebuildToolTips()
        needsDisplay = true
        onScaleChanged?()   // let the panel resize to fit
    }

    @objc private func turnOffPet() {
        guard let tile = contextTile else { return }
        onTurnOff?(tile)
    }

    private func tile(at point: NSPoint) -> PetTile? {
        // Points arrive in (scaled-up) view space; map back to logical tile space.
        let logical = NSPoint(x: point.x / scale, y: point.y / scale)
        let step = Self.tileWidth + Self.tileGap
        let index = Int(logical.x / step)
        guard tiles.indices.contains(index) else { return nil }
        let rect = NSRect(x: CGFloat(index) * step, y: 0, width: Self.tileWidth, height: Self.tileHeight)
        return rect.contains(logical) ? tiles[index] : nil
    }

    private func instance(at point: NSPoint) -> ProviderActivityInstance? {
        tile(at: point)?.instance
    }

    private func rebuildToolTips() {
        toolTipTags.forEach(removeToolTip)
        toolTipTags = tiles.indices.map { index in
            let x = CGFloat(index) * (Self.tileWidth + Self.tileGap) * scale
            return addToolTip(NSRect(x: x, y: 0, width: Self.tileWidth * scale, height: Self.tileHeight * scale), owner: self, userData: nil)
        }
    }

    func view(_ view: NSView, stringForToolTip tag: NSView.ToolTipTag, point: NSPoint, userData data: UnsafeMutableRawPointer?) -> String {
        guard let tile = tile(at: point) else { return "Provider quotas" }
        let instance = tile.instance
        if instance.key == "needs-login" {
            return instance.profile == "No source"
                ? "Nothing enabled — enable Hermes or Local"
                : "\(instance.profile) disconnected — sign in to see quotas"
        }
        let state: String
        switch instance.status {
        case "error", "failed": state = "Not working"
        case "waiting":         state = "Needs attention"
        case "recovered":       state = "Quota back"
        case "completed":       state = "Done"
        case "sleeping":        state = "Idle"
        default:                state = "Working"
        }
        // With several sessions, one pet stands in for all of them — name the count
        // and the provider they use; otherwise show the single session's detail.
        if tile.sessionCount > 1 {
            let provider = instance.providerLabel.isEmpty ? "" : "\(instance.providerLabel) "
            return "\(instance.profile) · \(tile.sessionCount) \(provider)sessions · \(state)"
        }
        let detail = instance.title.isEmpty ? "" : " · \(instance.title)"
        return "\(instance.profile)\(detail) · \(state)"
    }

    private func profileColor(_ profile: String) -> NSColor {
        var hash: UInt32 = 0
        for scalar in profile.unicodeScalars {
            hash = hash &* 31 &+ UInt32(scalar.value)
        }
        return NSColor(calibratedHue: CGFloat(hash % 360) / 360, saturation: 0.62, brightness: 0.94, alpha: 1)
    }

    private func drawNukey(_ instance: ProviderActivityInstance, image: NSImage?, thumbnail: NSImage?, rowFrames: [Int], in destination: NSRect) {
        // No artificial per-frame position offset in ANY state — the pet moves
        // purely through its own sprite frames, so it animates smoothly and never
        // shakes/jitters. (The error/failed state used to add a ±1.8px left-right
        // wobble every frame — that was the "shaking"; the waiting/working states
        // added sine bobs — all removed so every state just "moves properly".)
        let target = destination
        if let image {
            let frameWidth = Self.frameW
            let frameHeight = Self.frameH
            let now = Date().timeIntervalSince1970 * 1000
            // Canonical Hermes/petdex row taxonomy (agent/pet/constants
            // CODEX_STATE_ROWS): every pet's sheet lays its rows out the same
            // way, so this mapping animates ANY pet correctly — nukey, cosmo, …
            //   0 idle · 1 running-right · 2 running-left · 3 waving
            //   4 jumping · 5 failed · 6 waiting · 7 running · 8 review
            let row: Int
            switch instance.status {
            case "working":            row = 7   // running → actively working
            case "waiting":            row = 3   // needs your input → WAVE for attention
            case "recovered":          row = 3   // quota just reset → WAVE to celebrate
            case "error", "failed":    row = 5   // failed → in trouble
            case "completed":
                // celebrate (jump) briefly, then settle back to idle
                row = (now - (instance.completedAt ?? now) < 1_800) ? 4 : 0
            case "sleeping":           row = 0   // dormant/idle → the pet's own idle
            default:                   row = 0   // idle
            }
            // Step across only the frames this row actually holds (left-packed),
            // so a short row (e.g. the duck's 4-frame idle) loops cleanly instead
            // of stepping into blank cells. Fall back to 6 if we couldn't measure.
            let rowFrameCount = max(rowFrames.indices.contains(row) ? rowFrames[row] : 6, 1)
            // Advance one frame every 2 redraw ticks — a steady ~5 fps where every
            // frame is held for exactly the same time, so the walk/idle cycle reads
            // as lively but even, not staggered or rushed. (1680/2 = 840 is a
            // multiple of every frame count, so the loop stays even across the wrap.)
            let frame = (phase / 2) % rowFrameCount
            let source = NSRect(
                x: CGFloat(frame) * frameWidth,
                y: image.size.height - CGFloat(row + 1) * frameHeight,
                width: frameWidth,
                height: frameHeight
            )
            let previousInterpolation = NSGraphicsContext.current?.imageInterpolation
            NSGraphicsContext.current?.imageInterpolation = .none
            image.draw(in: target, from: source, operation: .sourceOver, fraction: 1, respectFlipped: false, hints: nil)
            if let previousInterpolation {
                NSGraphicsContext.current?.imageInterpolation = previousInterpolation
            }
            return
        }
        thumbnail?.draw(in: target, from: .zero, operation: .sourceOver, fraction: 1)
    }

    private func drawPet(_ tile: PetTile, rect tileRect: NSRect) {
        let instance = tile.instance
        let sheet = tile.pet.flatMap { sheetCache[$0.id] }
        let thumb = tile.pet.flatMap { thumbCache[$0.id] }
        let rowFrames = tile.pet.flatMap { frameCounts[$0.id] } ?? []
        let working = instance.status == "working"
        let failed = instance.failed
        let recovered = instance.status == "recovered"
        // Pet sits in the upper part; the bottom ~14pt is a dedicated dots row.
        let petRect = NSRect(x: tileRect.minX + 2, y: tileRect.minY + 16, width: 52, height: 56.3)
        let halo = NSBezierPath(ovalIn: NSRect(x: petRect.minX + 4, y: petRect.minY + 5, width: petRect.width - 8, height: petRect.height - 10))
        // Hermes blue is the primary halo accent; status recolours it.
        // "waiting" (needs-your-input) deliberately keeps the NORMAL halo — the pet
        // signals input only by WAVING (drawNukey row 3), never with a pet badge/tint;
        // the input badge lives on the session point instead.
        let haloColor: NSColor = failed ? .hermesRed : (instance.completed || recovered) ? .hermesGreen : .hermesBlue
        let haloPulse = CGFloat((sin(Double(phase) * .pi / 6) + 1) * 0.035)
        haloColor.withAlphaComponent((working || failed || recovered ? 0.17 : 0.09) + haloPulse).setFill()
        halo.fill()
        drawNukey(instance, image: sheet, thumbnail: thumb, rowFrames: rowFrames, in: petRect)

        // One mark per session, in a dedicated row ALONG THE BOTTOM (below the pet,
        // on a dark backing pill so it stands out against the pet's own colour): a
        // dot per session in its provider colour — bright while busy, dim while
        // idle — or a green check for a session that just finished.
        if !tile.sessions.isEmpty {
            // Each session is a GROUP of small dots — one per model it's running (a
            // session can use several: mixture-of-agents / fallback / switch) —
            // packed tightly, with a wider gap BETWEEN sessions. So two models in
            // one session read as a pair sitting together, distinct from a second
            // session's dots. Total dots capped so the row fits under the pet.
            let dotSize: CGFloat = 6
            let innerGap: CGFloat = 1.5   // between models within a session
            let padX: CGFloat = 2         // capsule padding — a tight circle around one dot
            let capH: CGFloat = 10        // capsule height — hug the dots so the glow sits close
            let cy = tileRect.minY + 10   // clear of the bottom edge so the (bigger) glow isn't clipped
            var groups: [(colors: [NSColor], busy: Bool, needsInput: Bool, needsPermission: Bool)] = []
            var total = 0
            for mark in tile.sessions {
                var pairs = mark.hex.split(separator: ",").compactMap { part -> NSColor? in
                    NSColor(activityHex: String(part).lowercased())
                }
                if pairs.isEmpty { pairs = [.hermesBlue] }
                if !groups.isEmpty && total + pairs.count > 7 { break }
                groups.append((pairs, mark.busy, mark.needsInput, mark.needsPermission))
                total += pairs.count
                if total >= 7 { break }
            }
            func groupW(_ count: Int) -> CGFloat { CGFloat(count) * dotSize + CGFloat(max(0, count - 1)) * innerGap }
            func capW(_ count: Int) -> CGFloat { groupW(count) + padX * 2 }
            // Sessions sit as a tight cluster centred under the pet. The gap BETWEEN
            // sessions ADAPTS to how many there are — a small gap for a couple,
            // tighter when there are more — so the row always fits inside the pet's
            // FIXED tile width (the pet never widens to fit the dots).
            let capsTotal = groups.reduce(CGFloat(0)) { $0 + capW($1.colors.count) }
            let sessionGap: CGFloat = {
                guard groups.count > 1 else { return 0 }
                let fit = (Self.tileWidth - 6 - capsTotal) / CGFloat(groups.count - 1)
                return max(1, min(3, fit))
            }()
            let rowW = capsTotal + CGFloat(max(0, groups.count - 1)) * sessionGap
            var cx0 = tileRect.midX - rowW / 2   // left edge of the first capsule
            // Small orange "input" badge at a point's top-right (waiting for you) —
            // sits mostly OUTSIDE the point so its own colour still shows.
            // A "needs you" ICON at the point's TOP-RIGHT (no ring) — same amber chip
            // across ALL providers so it reads consistently, with a thin dark rim so
            // it pops on any point colour. Shape says which kind: a caret for input.
            func drawInputBadge(_ dotRect: NSRect) {
                let bd: CGFloat = 7
                let br = NSRect(x: dotRect.maxX - 2.4, y: dotRect.maxY - 2.4, width: bd, height: bd)
                NSColor.black.withAlphaComponent(0.35).setStroke()
                let rim = NSBezierPath(ovalIn: br.insetBy(dx: -0.5, dy: -0.5)); rim.lineWidth = 1; rim.stroke()
                NSColor.hermesYellow.setFill()
                NSBezierPath(ovalIn: br).fill()
                NSColor.white.withAlphaComponent(0.95).setStroke()
                let outline = NSBezierPath(ovalIn: br); outline.lineWidth = 0.7; outline.stroke()
                // white input caret (text cursor) — "your turn".
                NSColor.white.setFill()
                NSBezierPath(roundedRect: NSRect(x: br.midX - 0.65, y: br.midY - 2, width: 1.3, height: 4),
                             xRadius: 0.65, yRadius: 0.65).fill()
            }
            // Permission / approval prompt — the SAME amber chip, but a white LOCK.
            func drawPermissionBadge(_ dotRect: NSRect) {
                let bd: CGFloat = 7
                let br = NSRect(x: dotRect.maxX - 2.4, y: dotRect.maxY - 2.4, width: bd, height: bd)
                NSColor.black.withAlphaComponent(0.35).setStroke()
                let rim = NSBezierPath(ovalIn: br.insetBy(dx: -0.5, dy: -0.5)); rim.lineWidth = 1; rim.stroke()
                NSColor.hermesYellow.setFill()
                NSBezierPath(ovalIn: br).fill()
                NSColor.white.withAlphaComponent(0.95).setStroke()
                let outline = NSBezierPath(ovalIn: br); outline.lineWidth = 0.7; outline.stroke()
                // mini lock: a white body + a thin white shackle arc on top.
                NSColor.white.setFill()
                let body = NSRect(x: br.midX - 1.5, y: br.minY + 1.7, width: 3, height: 2.2)
                NSBezierPath(roundedRect: body, xRadius: 0.6, yRadius: 0.6).fill()
                NSColor.white.setStroke()
                let shackle = NSBezierPath()
                shackle.appendArc(withCenter: NSPoint(x: br.midX, y: body.maxY), radius: 0.95, startAngle: 0, endAngle: 180)
                shackle.lineWidth = 0.6
                shackle.stroke()
            }
            for (gi, group) in groups.enumerated() {
                let w = capW(group.colors.count)
                // "Running" glows the WHOLE session capsule in sync and ENLARGES it on
                // a ~1s beat, so a multi-model session's adjacent dots share ONE
                // coherent glow. The capsule is a circle for a single model, a
                // horizontal pill for several; the dots inside keep constant colours.
                let g = group.busy ? CGFloat((sin(Double(phase) * .pi / 5 + Double(gi)) + 1) * 0.5) : 0
                // Subtle dark backing pill groups a session's dots — no glow / no border
                // on the pill itself; the GLOW is on each POINT (below), in the point's
                // OWN provider colour, matching the menu-bar dots.
                let cap = NSRect(x: cx0, y: cy - capH / 2, width: w, height: capH)
                let capPath = NSBezierPath(roundedRect: cap, xRadius: cap.height / 2, yRadius: cap.height / 2)
                NSColor.black.withAlphaComponent(group.busy ? 0.42 : 0.3).setFill()
                capPath.fill()

                // Each point glows in ITS OWN colour: a soft colour wash + a shadow-glow
                // that pulses + enlarges on the beat — one glowing dot for a single model,
                // two adjacent glowing dots (each its colour) for a multi-model session.
                var dx = cx0 + padX
                for color in group.colors {
                    let dotRect = NSRect(x: dx, y: cy - dotSize / 2, width: dotSize, height: dotSize)
                    if group.busy {
                        color.withAlphaComponent(0.22).setFill()
                        NSBezierPath(ovalIn: dotRect.insetBy(dx: -2.5 - g, dy: -2.5 - g)).fill()
                        NSGraphicsContext.saveGraphicsState()
                        let shadow = NSShadow()
                        shadow.shadowColor = color.withAlphaComponent(0.95)
                        shadow.shadowBlurRadius = 2.5 + 2.5 * g
                        shadow.shadowOffset = .zero
                        shadow.set()
                        color.setFill()
                        NSBezierPath(ovalIn: dotRect).fill()
                        NSBezierPath(ovalIn: dotRect).fill()
                        NSGraphicsContext.restoreGraphicsState()
                    }
                    color.setFill()
                    NSBezierPath(ovalIn: dotRect).fill()
                    let dot = NSBezierPath(ovalIn: dotRect)
                    NSColor.white.withAlphaComponent(0.85).setStroke()
                    dot.lineWidth = 0.75
                    dot.stroke()
                    if group.needsPermission { drawPermissionBadge(dotRect) }
                    else if group.needsInput { drawInputBadge(dotRect) }
                    dx += dotSize + innerGap
                }
                cx0 += w + sessionGap
            }
        }

        // Status badge, top-right (kept clear of the bottom dots row). ONLY the error
        // ("!") state badges the pet — "needs your input" is shown by the pet WAVING
        // plus the input badge on the session point, never a pet badge.
        if failed {
            let pulse = CGFloat((sin(Double(phase) * .pi / 4) + 1) * 1.2)
            let badge = NSBezierPath(ovalIn: NSRect(x: tileRect.maxX - 16 - pulse / 2, y: tileRect.maxY - 16 - pulse / 2, width: 14 + pulse, height: 14 + pulse))
            NSColor.hermesRed.setFill()
            badge.fill()
            let attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 9, weight: .bold),
                .foregroundColor: NSColor.white
            ]
            NSAttributedString(string: "!", attributes: attributes).draw(at: NSPoint(x: tileRect.maxX - 10.6, y: tileRect.maxY - 14.6))
        } else if instance.completed {
            let badge = NSBezierPath(ovalIn: NSRect(x: tileRect.maxX - 16, y: tileRect.maxY - 16, width: 14, height: 14))
            NSColor.hermesGreen.setFill()
            badge.fill()
            let check = NSBezierPath()
            check.move(to: NSPoint(x: tileRect.maxX - 12.5, y: tileRect.maxY - 9))
            check.line(to: NSPoint(x: tileRect.maxX - 9.7, y: tileRect.maxY - 12))
            check.line(to: NSPoint(x: tileRect.maxX - 5, y: tileRect.maxY - 6.5))
            NSColor.white.setStroke()
            check.lineWidth = 1.8
            check.lineCapStyle = .round
            check.lineJoinStyle = .round
            check.stroke()
        }

        // The per-session dots show up to 8; beyond that, a count badge (top-left,
        // provider colour) gives the true total.
        if tile.sessionCount > 8 {
            let color = NSColor(activityHex: instance.providerColor) ?? .hermesBlue
            let d: CGFloat = 16
            let rect = NSRect(x: tileRect.minX + 1, y: tileRect.maxY - d - 1, width: d, height: d)
            let badge = NSBezierPath(ovalIn: rect)
            color.setFill(); badge.fill()
            NSColor.white.withAlphaComponent(0.9).setStroke(); badge.lineWidth = 1; badge.stroke()
            let text = tile.sessionCount > 9 ? "9+" : "\(tile.sessionCount)"
            let attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: tile.sessionCount > 9 ? 8 : 9.5, weight: .bold),
                .foregroundColor: NSColor.white
            ]
            let string = NSAttributedString(string: text, attributes: attributes)
            let size = string.size()
            string.draw(at: NSPoint(x: rect.midX - size.width / 2, y: rect.midY - size.height / 2))
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSGraphicsContext.saveGraphicsState()
        if scale != 1 {
            let transform = NSAffineTransform()
            transform.scale(by: scale)
            transform.concat()
        }
        for (index, tile) in tiles.enumerated() {
            let x = CGFloat(index) * (Self.tileWidth + Self.tileGap)
            drawPet(tile, rect: NSRect(x: x, y: 0, width: Self.tileWidth, height: Self.tileHeight))
        }
        NSGraphicsContext.restoreGraphicsState()
    }
}

final class ActivityPetsPanel: NSPanel {
    private let petsView = ActivityPetsView(frame: .zero)
    private var placed = false
    var onSelect: ((ProviderActivityInstance) -> Void)? {
        didSet {
            petsView.onSelect = onSelect
        }
    }
    var onTurnOff: ((PetTile) -> Void)? {
        didSet {
            petsView.onTurnOff = onTurnOff
        }
    }

    init() {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: ActivityPetsView.tileWidth, height: ActivityPetsView.tileHeight),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        level = .floating
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        ignoresMouseEvents = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        acceptsMouseMovedEvents = true
        contentView = petsView
        petsView.onMove = { [weak self] deltaX, deltaY in
            guard let self else { return }
            setFrameOrigin(NSPoint(x: frame.origin.x + deltaX, y: frame.origin.y + deltaY))
            UserDefaults.standard.set(frame.origin.x, forKey: "activityPetsX")
            UserDefaults.standard.set(frame.origin.y, forKey: "activityPetsY")
        }
        petsView.onScaleChanged = { [weak self] in self?.relayout() }
    }

    // Re-fit the panel to the current tiles at the current scale (after Enlarge).
    func relayout() {
        guard !petsView.tiles.isEmpty else { return }
        show(petsView.tiles)
    }

    private func constrainedOrigin(_ origin: NSPoint, size: NSSize) -> NSPoint {
        let screen = NSScreen.screens.first { $0.frame.contains(origin) } ?? NSScreen.main ?? NSScreen.screens.first
        guard let visible = screen?.visibleFrame else { return origin }
        return NSPoint(
            x: min(max(origin.x, visible.minX), max(visible.minX, visible.maxX - size.width)),
            y: min(max(origin.y, visible.minY), max(visible.minY, visible.maxY - size.height))
        )
    }

    func show(_ tiles: [PetTile]) {
        let visible = Array(tiles.prefix(8))
        guard !visible.isEmpty else {
            orderOut(nil)
            return
        }
        let logicalWidth = CGFloat(visible.count) * ActivityPetsView.tileWidth + CGFloat(max(0, visible.count - 1)) * ActivityPetsView.tileGap
        let scale = petsView.scale
        let size = NSSize(width: logicalWidth * scale, height: ActivityPetsView.tileHeight * scale)
        let previousOrigin = frame.origin
        petsView.frame = NSRect(origin: .zero, size: size)
        petsView.tiles = visible
        setContentSize(size)
        let origin: NSPoint
        if placed {
            origin = previousOrigin
        } else if UserDefaults.standard.object(forKey: "activityPetsX") != nil {
            origin = NSPoint(
                x: UserDefaults.standard.double(forKey: "activityPetsX"),
                y: UserDefaults.standard.double(forKey: "activityPetsY")
            )
        } else if let frame = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            origin = NSPoint(x: frame.maxX - size.width - 18, y: frame.minY + 18)
        } else {
            origin = previousOrigin
        }
        let constrained = constrainedOrigin(origin, size: size)
        setFrameOrigin(constrained)
        UserDefaults.standard.set(constrained.x, forKey: "activityPetsX")
        UserDefaults.standard.set(constrained.y, forKey: "activityPetsY")
        placed = true
        orderFrontRegardless()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    // Sources: `.hermes` = the Hermes Desktop app's gateway session (via
    // hermes-desktop-quotas); `.local` = the locally-authenticated providers on
    // this Mac (via hermes-local-quotas). The user enables one or BOTH; when both
    // are enabled the menu shows each source's providers together (the same
    // provider, e.g. Claude, can appear once per source). No fallback.
    private enum GatewayKind: String { case hermes, local }
    // One entry per enabled source with the providers it returned (or synthetic
    // "Disconnected" rows if that source is down).
    private struct SourceQuota {
        let kind: GatewayKind
        let providers: [QuotaProvider]
        let connected: Bool
        let generatedAt: String?
    }
    private var sources: [SourceQuota] = []
    // Sources just switched on and awaiting their first fetch — shown with a
    // spinner ("Activating …") until the fetch that follows completes.
    private var activating: Set<GatewayKind> = []
    // Sources being re-checked via a provider row's Refresh — that row shows a
    // spinner in place of its refresh icon until the fetch returns.
    private var refreshingKinds: Set<GatewayKind> = []
    private var desktopStatus = DesktopStatus(running: false, mode: "unknown", authMode: "unknown", remoteURL: nil, signedIn: false, reachable: false, profile: nil, provider: nil, model: nil)
    private var refreshing = false
    private var timer: Timer?
    private var authRefreshTimers: [Timer] = []
    private var signalSource: DispatchSourceSignal?
    // Providers whose detail is expanded (collapsed by default → brief row with a
    // bar; click to expand for per-window info). Session-scoped.
    private var expandedProviders = Set<String>()
    // Provider keys observed OUT OF QUOTA on the latest connected reading — so a
    // later reading that has quota again is recognised as a RESET (its window
    // rolled over), not a first sighting. Only connected+ok readings mutate this.
    private var exhaustedProviderKeys = Set<String>()
    // When a provider's quota last came BACK (exhausted → has quota). Drives the
    // brief "quota back" celebration: a green reset icon on its row + the source's
    // pet WAVING. Cleared implicitly once older than `quotaRecoveryCelebration`.
    private var quotaRecoveredAt: [String: Date] = [:]
    private static let quotaRecoveryCelebration: TimeInterval = 8
    // Whether the Sources selector is expanded to show its per-source checkboxes.
    private var sourcesExpanded = false
    // Last provider slug→label from a successful fetch, PER source, so a
    // disconnected (or freshly-switched) source lists only its own providers, and
    // disabling a source can drop its providers entirely.
    private var lastProvidersByKind: [String: [(slug: String, label: String)]] = {
        var out: [String: [(slug: String, label: String)]] = [:]
        for kind in ["hermes", "local"] {
            let slugs = UserDefaults.standard.stringArray(forKey: "lastProviderSlugs.\(kind)") ?? []
            let labels = UserDefaults.standard.stringArray(forKey: "lastProviderLabels.\(kind)") ?? []
            if !slugs.isEmpty { out[kind] = zip(slugs, labels).map { ($0, $1) } }
        }
        return out
    }()
    private let activityPanel = ActivityPetsPanel()
    private var activityTimer: Timer?
    // Cached Hermes gateway activity (polled from /api/status), driving the Hermes
    // pet's working state — works for a remote gateway where sessions run server-side.
    private var hermesBusy = false
    private var hermesActiveAgents = 0
    // Provider colour (hex) per ACTIVE Hermes session, so its dots match the real
    // provider (e.g. OpenRouter purple) instead of a generic gateway colour.
    private var hermesSessionHexes: [String] = []
    // Provider colour (hex) per Hermes session the gateway flagged as WAITING FOR
    // YOUR INPUT — drawn as a waiting dot with an input badge, and it makes the pet
    // wave for attention.
    private var hermesWaitingHexes: [String] = []
    // Provider colour (hex) per Hermes session the gateway flagged as awaiting a
    // PERMISSION / approval prompt — a lock badge on the point (and the pet waves).
    private var hermesPermissionHexes: [String] = []
    // Last local session scan, cached so an eye toggle can rebuild the pet
    // synchronously (immediately) without waiting for the next 1s scan.
    private var lastLocalSessions: [LocalSession] = []
    private var hermesActivityTimer: Timer?
    private var hermesPollInFlight = false

    private func sourceName(_ kind: GatewayKind) -> String {
        kind == .local ? "Local" : "Hermes"
    }

    // Any enabled source returned data.
    private var anyConnected: Bool {
        sources.contains { $0.connected }
    }

    // Every enabled source is down (or none is enabled) — drives the "not
    // working" pet + a sign-in prompt.
    private var needsLogin: Bool {
        !enabledGateways().isEmpty && !anyConnected
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        applyAppearance()   // restore the saved app theme before building any views
        menu.delegate = self
        statusItem.menu = menu
        activityPanel.onSelect = { [weak self] instance in
            self?.openHermesSession(instance)
        }
        // Right-click "Turn Off" on a pet disables that source's pet and refreshes
        // both the floating pets and the menu's paw toggle so they stay in sync.
        activityPanel.onTurnOff = { [weak self] tile in
            guard let self, !tile.sourceKey.isEmpty else { return }
            PetCatalog.setPetEnabled(false, forKey: tile.sourceKey)
            self.updateActivityPets()
            self.rebuildMenu()
        }
        syncPetsFromGateway()
        updateStatusItem()
        rebuildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.refresh()
            self?.syncPetsFromGateway()  // pick up newly-installed gateway pets
        }
        updateActivityPets()
        // Poll local usage every 1s so the Local pet turns active as soon as a
        // `claude` session is busy.
        activityTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.updateActivityPets()
        }
        // Poll the Hermes gateway often (the helper's --activity round-trip is
        // ~0.3s), so a short turn's dot APPEARS and CLEARS promptly instead of
        // being missed between sparse polls (the "OpenRouter point never showed"
        // symptom — a fast turn can start and finish inside a 2s gap once poll
        // phase and a slow round-trip line up).
        pollHermesActivity()
        hermesActivityTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.pollHermesActivity()
        }
        signal(SIGUSR1, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
        source.setEventHandler { [weak self] in
            self?.statusItem.button?.performClick(nil)
        }
        source.resume()
        signalSource = source
    }

    func applicationWillTerminate(_ notification: Notification) {
        activityTimer?.invalidate()
        hermesActivityTimer?.invalidate()
        activityPanel.orderOut(nil)
    }

    func menuWillOpen(_ menu: NSMenu) {
        rebuildMenu()
        applyMenuWindowAppearance()   // theme the dropdown to match the forced mode
        refreshDesktopStatus()
        syncPetsFromGateway()  // reflect a pet installed since the last open
    }

    private func updateStatusItem() {
        guard let button = statusItem.button else { return }
        button.image = statusDotsImage()
        button.imagePosition = .imageOnly
        button.title = ""
        button.toolTip = statusTooltip()
        button.setAccessibilityLabel("Provider quota status")
    }

    // `color: nil` → the theme-aware PRIMARY colour (explicit dark grey in a light
    // theme, so text never renders light-on-light regardless of appearance cascade).
    private func label(_ text: String, frame: NSRect, font: NSFont, color: NSColor? = nil, alignment: NSTextAlignment = .left) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.frame = frame
        field.font = font
        field.textColor = color ?? menuPrimaryColor()
        field.alignment = alignment
        field.lineBreakMode = .byTruncatingTail
        return field
    }

    private func menuMaterialView(_ frame: NSRect) -> NSView {
        // ONE glass for every mode: the native NSMenu vibrancy the menu already has.
        // Rows stay transparent (no extra NSVisualEffectView layered on top — that
        // second layer was the "frost panel in front"). A forced theme just RE-COLOURS
        // that native glass by overriding the menu window's appearance
        // (ThemedMenuContainer), so Light and Dark share the exact same glass and only
        // the colour switches; System follows the OS. Text uses the fixed theme-aware
        // colours so it stays readable on either.
        let view = ThemedMenuContainer(frame: frame)
        view.forcedAppearance = themedAppearance()
        view.appearance = themedAppearance()
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.clear.cgColor
        return view
    }

    private func addView(_ view: NSView) {
        let item = NSMenuItem()
        item.view = view
        menu.addItem(item)
    }

    private func titleView() -> NSView {
        // Only surface the source-connection line when it's ACTIONABLE — a source is
        // down, needs sign-in, or nothing is enabled. When everything's connected the
        // "Hermes connected · Local connected" line was just noise, so drop it and
        // tighten the header. (Per-source headers below still carry each source's state.)
        let allConnected = !enabledGateways().isEmpty
            && enabledGateways().allSatisfy { kind in sources.first { $0.kind == kind }?.connected ?? false }
        let showConnection = !allConnected
        let height: CGFloat = showConnection ? 68 : 50
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: height))
        view.addSubview(label("Provider Quotas", frame: NSRect(x: 16, y: showConnection ? 39 : 26, width: 230, height: 20), font: .systemFont(ofSize: 15, weight: .semibold), color: menuPrimaryColor()))
        if showConnection {
            view.addSubview(label(connectionSummary(), frame: NSRect(x: 16, y: 20, width: 300, height: 17), font: .systemFont(ofSize: 11, weight: .medium), color: anyConnected ? .hermesGreen : .hermesRed))
        }
        let generatedAt = sources.compactMap { $0.generatedAt }.first
        let updated = formattedRelativeDate(generatedAt).map { "Updated \($0)" } ?? "Provider quotas"
        view.addSubview(label(updated, frame: NSRect(x: 16, y: showConnection ? 4 : 6, width: 300, height: 16), font: .systemFont(ofSize: 10.5), color: menuSecondaryColor()))
        let image = NSImageView(frame: NSRect(x: 320, y: showConnection ? 28 : 16, width: 22, height: 22))
        image.image = NSImage(systemSymbolName: "gauge.with.dots.needle.50percent", accessibilityDescription: "Quotas")
        image.contentTintColor = .hermesBlue
        view.addSubview(image)
        return view
    }

    // Stable per-(source,provider) key so the same provider from two sources
    // expands independently.
    private func providerKey(_ kind: GatewayKind, _ slug: String) -> String { "\(kind.rawValue):\(slug)" }

    // Collapsed provider row: brief summary + a provider-coloured bar (overall
    // remaining). Click toggles the expanded per-window detail. `connected` is the
    // provider's SOURCE state (so a provider from a down source reads red even if
    // the other source is up).
    // Colours of the sessions running RIGHT NOW for a source (the same feed that
    // lights the pet). Used to glow an "effective" (actively-running) provider in
    // the menu, matching the pet.
    private func activeSessionColors(_ kind: GatewayKind) -> Set<String> {
        var colors = Set<String>()
        if kind == .hermes {
            for hex in hermesSessionHexes {
                for c in hex.split(separator: ",") { colors.insert(String(c).lowercased()) }
            }
        } else {
            for s in lastLocalSessions where s.busy { colors.insert(s.hex.lowercased()) }
        }
        return colors
    }

    private func providerActive(_ kind: GatewayKind, _ slug: String) -> Bool {
        activeSessionColors(kind).contains(Self.providerHex(slug).lowercased())
    }

    private func providerView(_ provider: QuotaProvider, kind: GatewayKind, connected: Bool) -> NSView {
        let key = providerKey(kind, provider.provider)

        // Hidden via the eye → a minimal row that sits UNDER the shown providers
        // (rebuildMenu orders hidden ones last per source). It keeps only the
        // essentials — the provider's colour point, its NAME, and a dimmed
        // eye.slash — dropping all quota info; it isn't expandable, and the WHOLE
        // row is clickable to reveal the provider again (not only the eye). Kept
        // short so a stack of hidden providers takes little room.
        if !providerShownInMenuBar(kind, provider.provider) {
            let row = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 22))
            let dot = NSImageView(frame: NSRect(x: 18, y: 7, width: 8, height: 8))
            dot.image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
            dot.imageScaling = .scaleProportionallyDown
            dot.contentTintColor = providerBrandColor(provider)
            row.addSubview(dot)
            row.addSubview(label(provider.label, frame: NSRect(x: 32, y: 4, width: 250, height: 14),
                                 font: .systemFont(ofSize: 11, weight: .medium), color: menuTertiaryColor()))
            let eyeIcon = NSImageView(frame: NSRect(x: 300, y: 3, width: 16, height: 16))
            eyeIcon.image = NSImage(systemSymbolName: "eye.slash", accessibilityDescription: "Hidden — click the row to show")
            eyeIcon.imageScaling = .scaleProportionallyDown
            eyeIcon.contentTintColor = menuTertiaryColor()
            row.addSubview(eyeIcon)
            // Whole-row reveal button on top: click anywhere to show the provider.
            let reveal = NSButton(frame: row.bounds)
            reveal.isBordered = false
            reveal.title = ""
            reveal.identifier = NSUserInterfaceItemIdentifier(key)
            reveal.target = self
            reveal.action = #selector(toggleProviderMenuBar(_:))
            reveal.toolTip = "Show \(provider.label) again"
            row.addSubview(reveal)
            return row
        }

        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 48))
        let expanded = expandedProviders.contains(key)
        let chevron = NSImageView(frame: NSRect(x: 13, y: 18, width: 12, height: 12))
        chevron.image = NSImage(systemSymbolName: expanded ? "chevron.down" : "chevron.right", accessibilityDescription: expanded ? "Collapse" : "Expand")
        chevron.contentTintColor = menuTertiaryColor()
        view.addSubview(chevron)
        let symbol = providerSymbolName(provider.provider)
        let image = NSImageView(frame: NSRect(x: 32, y: 25, width: 16, height: 16))
        image.image = NSImage(systemSymbolName: symbol, accessibilityDescription: provider.label)
        // Always the provider's own colour — even out of quota or offline — so the
        // row reads consistently; the status dot (red ring) flags any problem.
        image.contentTintColor = providerBrandColor(provider)
        view.addSubview(image)
        view.addSubview(label(provider.label, frame: NSRect(x: 56, y: 26, width: 150, height: 18), font: .systemFont(ofSize: 13, weight: .semibold), color: providerBrandColor(provider)))
        if let plan = provider.plan {
            view.addSubview(label(plan.uppercased(), frame: NSRect(x: 200, y: 28, width: 90, height: 15), font: .systemFont(ofSize: 9, weight: .medium), color: menuTertiaryColor(), alignment: .right))
        }
        // A provider that just RESET swaps its status dot for a green reset icon and
        // leads its summary with "Quota back" for a few seconds (the pet waves too).
        let recovered = providerRecentlyRecovered(key)
        let statusImage = NSImageView(frame: NSRect(x: recovered ? 320 : 322, y: 26, width: recovered ? 17 : 14, height: recovered ? 17 : 14))
        if recovered {
            statusImage.image = NSImage(systemSymbolName: "arrow.clockwise.circle.fill", accessibilityDescription: "Quota reset — available again")
            statusImage.contentTintColor = .hermesGreen
            statusImage.imageScaling = .scaleProportionallyDown
        } else {
            statusImage.image = providerDotImage(provider, size: 14, connected: connected)
        }
        view.addSubview(statusImage)
        // Brief line: summary text + an overall provider-coloured bar. On a just-RESET
        // provider the green reset icon (above) is the recovery cue — the summary stays
        // NORMAL wording (no "Quota back ·" prefix, which was long enough to overrun the
        // bar) so it reads cleanly next to the restored bar, tinted green only while the
        // celebration lasts. When a bar WILL show, the summary is narrow so it can't
        // collide with it; an out-of-quota row (no bar) gets the full width for its
        // longer "Resets in … · over limit" text.
        let summaryText = providerSummary(provider, connected: connected)
        let barWillShow = connected && provider.status == "ok" && !providerIsExhausted(provider)
            && collapsedRemainingPercent(provider) != nil
        let summaryWidth: CGFloat = barWillShow ? 170 : 260
        view.addSubview(label(summaryText, frame: NSRect(x: 56, y: 7, width: summaryWidth, height: 15), font: .systemFont(ofSize: 10.5), color: recovered ? .hermesGreen : menuSecondaryColor()))
        // Bar tracks the collapsed %: the current-session window for %-based
        // providers (Codex/Claude); amount-only providers (OpenRouter) keep their
        // existing bar via the min fallback. An OUT-OF-QUOTA provider draws NO bar
        // (a 0%-full bar is just noise) — the summary carries its reset time
        // instead, the same treatment as the expanded window rows.
        if barWillShow, let minimum = collapsedRemainingPercent(provider) {
            let track = NSView(frame: NSRect(x: 232, y: 11, width: 104, height: 6))
            track.wantsLayer = true
            track.layer?.cornerRadius = 3
            track.layer?.backgroundColor = NSColor.separatorColor.withAlphaComponent(0.28).cgColor
            let fillWidth = 104 * max(0, min(100, minimum)) / 100
            let fill = NSView(frame: NSRect(x: 0, y: 0, width: fillWidth, height: 6))
            fill.wantsLayer = true
            fill.layer?.cornerRadius = 3
            fill.layer?.backgroundColor = barColor(for: provider, remainingPercent: minimum).cgColor
            track.addSubview(fill)
            view.addSubview(track)
        }
        let button = NSButton(frame: view.bounds)
        button.isBordered = false
        button.title = ""
        button.identifier = NSUserInterfaceItemIdentifier(key)
        button.target = self
        button.action = #selector(toggleProviderExpanded)
        button.toolTip = expanded ? "Collapse \(provider.label)" : "Expand \(provider.label)"
        view.addSubview(button)
        // Eye toggle: whether this provider's dot shows in the closed menu bar.
        // Added AFTER the full-row expand button so it stays independently
        // clickable on top of it.
        let shown = providerShownInMenuBar(kind, provider.provider)
        let eye = NSButton(frame: NSRect(x: 300, y: 24, width: 18, height: 18))
        eye.isBordered = false
        eye.title = ""
        eye.image = NSImage(systemSymbolName: shown ? "eye" : "eye.slash",
                            accessibilityDescription: shown ? "Shown in menu bar" : "Hidden from menu bar")
        eye.imagePosition = .imageOnly
        eye.imageScaling = .scaleProportionallyDown
        eye.contentTintColor = shown ? .hermesBlue : .tertiaryLabelColor
        eye.identifier = NSUserInterfaceItemIdentifier(key)
        eye.target = self
        eye.action = #selector(toggleProviderMenuBar(_:))
        eye.toolTip = shown ? "Hide \(provider.label) from the menu bar" : "Show \(provider.label) in the menu bar"
        view.addSubview(eye)
        return view
    }

    // Per-source header: the source's identity (name + connected/disconnected) on
    // the left, and a compact right-aligned cluster of that source's OWN controls
    // — its pet (paw toggles on/off, thumbnail cycles) plus, for Hermes, a shortcut
    // that opens the Hermes app (login/logout/settings are controlled in Hermes, not
    // here). One header per enabled source, so a source's controls sit right where
    // its connection state shows. Kept tight with small icon buttons.
    private func sourceHeaderView(_ source: SourceQuota) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 28))
        let kind = source.kind
        let name = sourceName(kind)
        let dot = NSImageView(frame: NSRect(x: 16, y: 10, width: 9, height: 9))
        dot.image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
        dot.contentTintColor = source.connected ? .hermesGreen : .hermesRed
        view.addSubview(dot)
        view.addSubview(label(name.uppercased(), frame: NSRect(x: 30, y: 8, width: 66, height: 14),
                              font: .systemFont(ofSize: 10, weight: .semibold), color: menuSecondaryColor()))
        // The dot's colour already conveys the connection state; a row tooltip
        // spells it out, so the width goes to clearly-labelled controls instead.
        view.toolTip = "\(name) — \(source.connected ? "connected" : "disconnected")"

        // Right-aligned cluster of this source's own controls — plain, uniform icon
        // buttons: a per-SOURCE Refresh, the pet (paw toggle + its picture to
        // switch), then — Hermes only — a shortcut that opens the Hermes app.
        let key = kind.rawValue
        let on = PetCatalog.petEnabled(forKey: key)
        let pet = PetCatalog.selected(forKey: key)
        var controls: [NSButton] = []

        if !PetCatalog.installed().isEmpty {
            // Pet picture (the switcher) sits to the LEFT of the paw on/off toggle.
            // `controls` is laid out left→right, so append the switcher first.
            if on, let pet, PetCatalog.installed().count > 1 {
                // The switcher IS the pet's picture, so you see what you'll get.
                controls.append(iconButton(image: PetCatalog.petImage(pet, size: 20), tint: nil,
                                           action: #selector(cycleSourcePet(_:)), key: key,
                                           tooltip: "Switch \(name)'s pet — now \(pet.displayName)"))
            }
            controls.append(iconButton(image: NSImage(systemSymbolName: on ? "pawprint.fill" : "pawprint", accessibilityDescription: nil),
                                       tint: on ? .hermesBlue : .tertiaryLabelColor, dim: !on,
                                       action: #selector(toggleSourcePet(_:)), key: key,
                                       tooltip: on ? "Turn off \(name)'s pet" : "Turn on \(name)'s pet"))
        }

        if kind == .hermes {
            // The menu does NOT control the gateway's login / logout / settings —
            // those live in Hermes itself. So instead of sign-in/out + setup
            // buttons, offer a single Hermes icon that just opens the Hermes app.
            let appURL = gatewayAppURL()
            let hermesIcon = appURL.map { NSWorkspace.shared.icon(forFile: $0.path) }
                ?? NSImage(systemSymbolName: "arrow.up.forward.app", accessibilityDescription: nil)
            hermesIcon?.size = NSSize(width: 20, height: 20)
            controls.append(iconButton(image: hermesIcon, tint: appURL == nil ? .hermesBlue : nil,
                                       action: #selector(openHermes), key: nil,
                                       tooltip: "Open Hermes"))
        }

        // Rightmost slot: the per-SOURCE Refresh (re-checks this whole source) —
        // a spinner while it's fetching. Then the other controls lay out to its
        // left, right-aligned, uniform 22pt with even gaps.
        var x: CGFloat = 344
        if refreshingKinds.contains(kind) {
            let spinner = NSProgressIndicator(frame: NSRect(x: x - 19, y: 5, width: 15, height: 15))
            spinner.style = .spinning
            spinner.controlSize = .small
            spinner.isIndeterminate = true
            spinner.usesThreadedAnimation = true
            spinner.startAnimation(nil)
            view.addSubview(spinner)
        } else {
            let refresh = iconButton(image: NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: nil),
                                     tint: .hermesBlue, action: #selector(refreshSourceAction(_:)),
                                     key: key, tooltip: "Refresh \(name)")
            refresh.frame = NSRect(x: x - 22, y: 3, width: 22, height: 22)
            view.addSubview(refresh)
        }
        x -= 26

        for button in controls.reversed() {
            x -= 22
            button.frame = NSRect(x: x, y: 3, width: 22, height: 22)
            view.addSubview(button)
            x -= 4
        }
        return view
    }

    // A plain, reliably-rendering icon button used across the header and provider
    // clusters. `tint` nil leaves a full-colour image (e.g. a pet picture) untinted.
    private func iconButton(image: NSImage?, tint: NSColor?, dim: Bool = false, action: Selector, key: String?, tooltip: String) -> NSButton {
        let button = NSButton()
        button.isBordered = false
        button.title = ""
        button.image = image
        button.imagePosition = .imageOnly
        button.imageScaling = .scaleProportionallyDown
        if let tint { button.contentTintColor = tint }
        button.alphaValue = dim ? 0.55 : 1.0
        button.target = self
        button.action = action
        if let key { button.identifier = NSUserInterfaceItemIdentifier(key) }
        button.toolTip = tooltip
        return button
    }

    // Bar colour = the provider's brand colour, turning red only when exhausted.
    private func barColor(for provider: QuotaProvider, remainingPercent: Double?) -> NSColor {
        if QuotaWindow.reachesLimit(remainingPercent: remainingPercent, remainingAmount: nil) {
            return .hermesRed
        }
        return providerBrandColor(provider)
    }

    // Synthetic provider rows for a disconnected source — from that source's
    // last-known provider list (or the default set if never connected).
    private func disconnectedProviders(for kind: GatewayKind) -> [QuotaProvider] {
        let cached = lastProvidersByKind[kind.rawValue] ?? []
        let base = cached.isEmpty
            ? [("openrouter", "OpenRouter"), ("anthropic", "Claude"), ("openai-codex", "Codex")]
            : cached.map { ($0.slug, $0.label) }
        return base.map { QuotaProvider(provider: $0.0, label: $0.1, status: "disconnected", plan: nil, windows: [], details: [], message: "Disconnected") }
    }

    // Smooth over TRANSIENT provider errors (e.g. Claude's usage API returning
    // HTTP 429 while Claude itself works fine locally): cache each provider's last
    // "ok" reading and, if a later fetch comes back not-ok, keep showing the cached
    // one for a while instead of flashing a red "issue". Persisted so it survives
    // relaunches. Returns the providers to actually display for this source.
    private static let lastGoodTTL: TimeInterval = 15 * 60

    private func applyLastGood(_ providers: [QuotaProvider], kind: GatewayKind) -> [QuotaProvider] {
        providers.map { provider in
            let dataKey = "goodQuota.\(providerKey(kind, provider.provider))"
            let atKey = "goodQuotaAt.\(providerKey(kind, provider.provider))"
            if provider.status == "ok" {
                if let data = try? JSONEncoder().encode(provider) {
                    UserDefaults.standard.set(data, forKey: dataKey)
                    UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: atKey)
                }
                return provider
            }
            // Non-ok fetch → show the last good reading if it's recent enough.
            let at = UserDefaults.standard.double(forKey: atKey)
            if at > 0, Date().timeIntervalSince1970 - at < Self.lastGoodTTL,
               let data = UserDefaults.standard.data(forKey: dataKey),
               let cached = try? JSONDecoder().decode(QuotaProvider.self, from: data) {
                return cached
            }
            return provider
        }
    }


    private func providerMinimum(_ provider: QuotaProvider) -> Double? {
        provider.windows.compactMap(\.remainingPercent).min()
    }

    // The "current session" window — the short (~5h) window whose remaining % the
    // COLLAPSED provider row shows (not the min across windows, which could be the
    // weekly/total). Identify it by a "session" label (Codex "Session", Claude
    // "Current session"), else the soonest-resetting window (a session resets
    // before the weekly), else the only window.
    private func sessionWindow(_ provider: QuotaProvider) -> QuotaWindow? {
        if let s = provider.windows.first(where: { $0.label.lowercased().contains("session") }) {
            return s
        }
        let dated = provider.windows.compactMap { w -> (QuotaWindow, Date)? in
            parsedDate(w.resetsAt).map { (w, $0) }
        }
        if let soonest = dated.min(by: { $0.1 < $1.1 })?.0 {
            return soonest
        }
        return provider.windows.first
    }

    // Remaining % of the current-session window (nil for amount-only providers
    // like OpenRouter, whose collapsed row shows a $ amount instead).
    private func sessionRemainingPercent(_ provider: QuotaProvider) -> Double? {
        sessionWindow(provider)?.remainingPercent
    }

    private func collapsedRemainingPercent(_ provider: QuotaProvider) -> Double? {
        provider.windows.filter(\.limitReached).compactMap(\.remainingPercent).min()
            ?? sessionRemainingPercent(provider)
            ?? providerMinimum(provider)
    }

    private func providerIsExhausted(_ provider: QuotaProvider) -> Bool {
        provider.status == "ok" && provider.windows.contains(where: \.limitReached)
    }

    // After each fetch, diff every connected+ok provider's exhaustion against the
    // last reading. A provider that WAS out of quota and now has some again just
    // reset — stamp it so its row and pet briefly celebrate. Disconnected / non-ok
    // readings are ignored (a source dropping offline isn't a reset), so a reset
    // that happens while a source is briefly down is still caught on reconnect.
    private func noteQuotaTransitions() {
        for source in sources where source.connected {
            for provider in source.providers where provider.status == "ok" {
                let key = providerKey(source.kind, provider.provider)
                if providerIsExhausted(provider) {
                    exhaustedProviderKeys.insert(key)
                } else if exhaustedProviderKeys.remove(key) != nil {
                    quotaRecoveredAt[key] = Date()
                }
            }
        }
    }

    // True while a provider is inside its post-reset celebration window.
    private func providerRecentlyRecovered(_ key: String) -> Bool {
        guard let at = quotaRecoveredAt[key] else { return false }
        return Date().timeIntervalSince(at) < Self.quotaRecoveryCelebration
    }

    // Labels of a source's providers still inside their celebration window — the
    // pet waves and names them while any is present.
    private func recoveredProviderLabels(_ kind: GatewayKind) -> [String] {
        (sources.first { $0.kind == kind }?.providers ?? []).compactMap { p in
            providerRecentlyRecovered(providerKey(kind, p.provider)) ? p.label : nil
        }
    }

    private func formattedAmount(_ amount: Double, currency: String?) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencyCode = currency ?? "USD"
        formatter.maximumFractionDigits = 2
        formatter.minimumFractionDigits = 2
        return formatter.string(from: NSNumber(value: amount)) ?? String(format: "$%.2f", amount)
    }

    // Canonical provider identity — one source of truth so a provider looks the
    // SAME across sources (Local vs Hermes) and in the pet. Aliases collapse to a
    // canonical slug, then colour/label/symbol are derived from that.
    static func normalizedProvider(_ slug: String) -> String {
        switch slug.lowercased() {
        case "anthropic", "claude", "claude-sub":          return "anthropic"
        case "openai-codex", "codex", "openai", "chatgpt": return "openai-codex"
        case "openrouter":                                 return "openrouter"
        case "opencode":                                   return "opencode"
        case "copilot", "copilot-acp", "github-copilot":   return "copilot"
        default:                                           return slug.lowercased()
        }
    }

    static func providerHex(_ slug: String) -> String {
        switch normalizedProvider(slug) {
        case "openrouter":   return "#8e1afe"   // purple
        case "opencode":     return "#38bdf8"   // sky blue (opencode's own OpenRouter key)
        case "anthropic":    return "#d97757"   // orange
        case "openai-codex": return "#10a37f"   // green
        case "copilot":      return "#6e7681"   // grey
        default:             return "#8e8e93"   // unknown → neutral grey
        }
    }

    static func providerLabel(_ slug: String) -> String {
        switch normalizedProvider(slug) {
        case "openrouter":   return "OpenRouter"
        case "opencode":     return "opencode"
        case "anthropic":    return "Claude"
        case "openai-codex": return "Codex"
        default:             return slug.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private func providerSymbolName(_ provider: String) -> String {
        switch Self.normalizedProvider(provider) {
        case "openrouter": return "arrow.triangle.branch"
        case "opencode":   return "terminal"
        case "anthropic":  return "sparkles"
        default:           return "chevron.left.forwardslash.chevron.right"   // Codex / other
        }
    }

    private func providerBrandColor(_ provider: QuotaProvider) -> NSColor {
        providerBrandColor(provider.provider)
    }

    private func providerBrandColor(_ provider: String) -> NSColor {
        NSColor(activityHex: Self.providerHex(provider))
            ?? NSColor(deviceRed: 16 / 255, green: 163 / 255, blue: 127 / 255, alpha: 1)
    }

    // Draws a provider status dot into the current context: the dot in the
    // provider's own colour, an optional GLOW (in that colour) while it's running a
    // session right now, and — when it's out of quota or not connected — a single
    // RED "buffer" ring outside it (replacing the healthy dot's white outline), so
    // the problem reads at a glance.
    private func drawProviderDot(in rect: NSRect, color: NSColor, ringColor: NSColor?, active: Bool = false, whiteOutline: Bool = true) {
        let s = rect.width
        let fillInset = ringColor != nil ? s * 0.26 : s * 0.12
        let dotRect = rect.insetBy(dx: fillInset, dy: fillInset)
        // A provider running a session RIGHT NOW glows its menu-bar dot in its own
        // colour — as prominent as the pet's per-point session glow.
        if active {
            // Soft colour wash so the glow reads clearly IN the provider's colour.
            color.withAlphaComponent(0.22).setFill()
            NSBezierPath(ovalIn: dotRect.insetBy(dx: -4, dy: -4)).fill()
            NSGraphicsContext.current?.saveGraphicsState()
            let shadow = NSShadow()
            shadow.shadowColor = color.withAlphaComponent(0.95)
            shadow.shadowBlurRadius = 4.2
            shadow.shadowOffset = .zero
            shadow.set()
            color.setFill()
            NSBezierPath(ovalIn: dotRect).fill()
            NSBezierPath(ovalIn: dotRect).fill()   // second pass strengthens the glow
            NSGraphicsContext.current?.restoreGraphicsState()
        }
        color.setFill()
        NSBezierPath(ovalIn: dotRect).fill()
        if let ringColor {
            // Alert (out of quota / not connected): a SINGLE red "buffer" ring
            // outside the dot — one clean line, no extra hugging outline.
            let ring = NSBezierPath(ovalIn: rect.insetBy(dx: s * 0.1, dy: s * 0.1))
            ringColor.setStroke()
            ring.lineWidth = max(2, s * 0.16)
            ring.stroke()
        } else if whiteOutline {
            // Healthy: a thin white outline hugging the dot — the pet's point style.
            // Skipped in the dropdown menu (plain dots there).
            let outline = NSBezierPath(ovalIn: dotRect)
            NSColor.white.withAlphaComponent(0.85).setStroke()
            outline.lineWidth = max(0.75, s * 0.09)
            outline.stroke()
        }
    }

    // The RED alert colour for a provider that's out of quota OR not connected —
    // used for BOTH the dot's outline and the outer ring; nil when it's fine. A
    // provider that's connected but momentarily can't read its usage (e.g. a 429)
    // is not an "issue", so it keeps its plain white-outlined dot.
    private func providerRingColor(_ provider: QuotaProvider, connected: Bool) -> NSColor? {
        if !connected || providerIsExhausted(provider) { return .hermesRed }
        return nil
    }

    private func providerDotImage(_ provider: QuotaProvider, size: CGFloat, connected: Bool) -> NSImage {
        let image = NSImage(size: NSSize(width: size, height: size))
        image.lockFocus()
        let ring = providerRingColor(provider, connected: connected)
        // Dropdown dots are plain (no white outline / no glow) — only the red alert
        // ring shows here; the pet-style outline+glow lives on the header + pets.
        drawProviderDot(in: NSRect(x: 0, y: 0, width: size, height: size),
                        color: providerBrandColor(provider), ringColor: ring, whiteOutline: false)
        image.unlockFocus()
        image.isTemplate = false
        image.accessibilityDescription = !connected ? "\(provider.label) is not connected — sign in" : ring != nil ? "\(provider.label) is out of quota" : "\(provider.label) is active"
        return image
    }


    // Shortest relative time until any of this provider's windows reset, e.g.
    // "in 6d". Used to surface the expiration in the collapsed provider row.
    private func soonestResetText(_ provider: QuotaProvider) -> String? {
        let dates = provider.windows.compactMap { parsedDate($0.resetsAt) }.filter { $0 > Date() }
        guard let soonest = dates.min() else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter.localizedString(for: soonest, relativeTo: Date())
    }

    // Relative reset ("in 4h") for a single window's resets_at, e.g. the session
    // window shown in the collapsed row.
    private func relativeReset(_ resetsAt: String) -> String? {
        guard let date = parsedDate(resetsAt), date > Date() else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private func providerSummary(_ provider: QuotaProvider, connected: Bool) -> String {
        // Per-provider status when the source is down, rather than repeating the
        // whole connection summary on every row.
        // Not connected (red ring) → say what to do: sign in.
        guard connected else { return "Disconnected · sign in" }
        guard provider.status == "ok" else {
            return provider.status.replacingOccurrences(of: "_", with: " ").capitalized
        }
        let reached = provider.windows.filter(\.limitReached)
        if !reached.isEmpty {
            // Out of quota (yellow ring). Reset FIRST (so it never gets truncated off
            // the row) — that reset time IS the thing to wait for, shown as a PRECISE
            // countdown ("in 2h 15m") to match the expanded window rows. When there's
            // no reset, credit-based providers just need a top-up, so say so.
            let soonest = reached
                .compactMap { w -> (String, Date)? in parsedDate(w.resetsAt).map { (w.resetsAt ?? "", $0) } }
                .filter { $0.1 > Date() }
                .min { $0.1 < $1.1 }
            if let soonest, let precise = preciseCountdown(soonest.0) {
                return "Resets in \(precise) · over limit"
            }
            if ["openrouter", "opencode"].contains(Self.normalizedProvider(provider.provider)) {
                return "Over limit · add credits"
            }
            return "Over limit"
        }
        // OpenRouter-shaped rows (the env/~/.hermes key AND opencode's own key)
        // carry a single "Account credits" window — summarise it as an amount.
        if ["openrouter", "opencode"].contains(Self.normalizedProvider(provider.provider)),
           let account = provider.windows.first(where: { $0.label == "Account credits" }),
           let amount = account.remainingAmount {
            return "\(formattedAmount(amount, currency: account.currency)) available"
        }
        // Collapsed row shows the CURRENT-SESSION window's % (not the min across
        // windows) — and the reset time of that same session window, so the number
        // and its reset match.
        if let session = sessionWindow(provider), let pct = session.remainingPercent {
            let base = pct <= 0 ? "No session quota" : "\(Int(pct.rounded()))% left"
            if let resetsAt = session.resetsAt, let reset = relativeReset(resetsAt) {
                return "\(base) · resets \(reset)"
            }
            return base
        }
        if let minimum = providerMinimum(provider) {
            let base = minimum <= 0 ? "No quota available" : "\(Int(minimum.rounded()))% left"
            if let reset = soonestResetText(provider) {
                return "\(base) · resets \(reset)"
            }
            return base
        }
        return "Quota available"
    }

    // Flattened (source, provider) pairs across all enabled sources.
    private func allEntries() -> [(kind: GatewayKind, connected: Bool, provider: QuotaProvider)] {
        sources.flatMap { source in source.providers.map { (source.kind, source.connected, $0) } }
    }

    private func statusDotsImage() -> NSImage {
        // Closed-menu circles are grouped BY SOURCE (section), with an extra gap
        // between sources so Hermes vs Local is distinguishable at a glance. Each
        // source contributes only the providers you haven't hidden with the eye.
        let step: CGFloat = 15      // per-dot advance
        let sectionGap: CGFloat = 9 // extra space between source sections
        let height: CGFloat = 21    // taller than the 11px dot so an active dot's glow fits
        let dotY: CGFloat = 5
        let lead: CGFloat = 6       // horizontal padding so an edge dot's glow isn't clipped
        let sections: [(kind: GatewayKind, connected: Bool, providers: [QuotaProvider])] = sources.compactMap { source in
            let visible = source.providers.filter { providerShownInMenuBar(source.kind, $0.provider) }
            return visible.isEmpty ? nil : (source.kind, source.connected, visible)
        }
        let dotCount = sections.reduce(0) { $0 + $1.providers.count }

        if dotCount == 0 {
            let image = NSImage(size: NSSize(width: 15, height: height))
            image.lockFocus()
            let color = anyConnected ? NSColor.tertiaryLabelColor : NSColor.hermesRed
            color.setStroke()
            let dot = NSBezierPath(ovalIn: NSRect(x: 2, y: dotY, width: 11, height: 11))
            dot.lineWidth = 2
            dot.stroke()
            image.unlockFocus()
            image.isTemplate = false
            return image
        }

        let width = CGFloat(dotCount) * step + CGFloat(max(0, sections.count - 1)) * sectionGap + lead * 2
        let image = NSImage(size: NSSize(width: width, height: height))
        image.lockFocus()
        var x: CGFloat = lead
        for (index, section) in sections.enumerated() {
            for provider in section.providers {
                drawProviderDot(in: NSRect(x: x, y: dotY, width: 11, height: 11),
                                color: providerBrandColor(provider.provider),
                                ringColor: providerRingColor(provider, connected: section.connected),
                                active: providerActive(section.kind, provider.provider))
                x += step
            }
            if index < sections.count - 1 { x += sectionGap }
        }
        image.unlockFocus()
        image.isTemplate = false
        return image
    }

    private func statusTooltip() -> String {
        let entries = allEntries()
        guard !entries.isEmpty else {
            if needsLogin || enabledGateways().isEmpty { return connectionSummary() }
            return refreshing ? "Loading provider quotas" : "Provider quotas unavailable"
        }
        let multi = sources.count > 1
        let providerText = entries.map { e in
            let prefix = multi ? "\(sourceName(e.kind)) " : ""
            return "\(prefix)\(e.provider.label): \(providerSummary(e.provider, connected: e.connected))"
        }.joined(separator: " · ")
        return "\(connectionSummary()) · \(providerText)"
    }

    // One-liner for the header / tooltips summarising the enabled sources.
    private func connectionSummary() -> String {
        let enabled = enabledGateways()
        if enabled.isEmpty { return "Nothing enabled — enable Hermes or Local" }
        if needsLogin {
            let names = enabled.map { sourceName($0) }.joined(separator: " and ")
            return "Sign in to \(names) to see quotas"
        }
        // Per-source status, e.g. "Hermes connected · Local disconnected".
        return enabled.map { kind in
            let connected = sources.first { $0.kind == kind }?.connected ?? false
            return "\(sourceName(kind)) \(connected ? "connected" : "disconnected")"
        }.joined(separator: " · ")
    }

    private func windowView(_ window: QuotaWindow, provider: QuotaProvider) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 66))
        let value = window.remainingPercent ?? 0
        let hasValue = window.remainingPercent != nil || window.remainingAmount != nil
        // The progress bar carries the PROVIDER's brand colour (red when
        // exhausted); the value TEXT stays high-contrast, warming to orange when
        // the quota is low so it stands out.
        let low = window.remainingPercent.map { $0 <= 15 } ?? false
        let bar = hasValue ? barColor(for: provider, remainingPercent: window.remainingPercent) : NSColor.tertiaryLabelColor
        let valueColor: NSColor = hasValue ? (window.limitReached ? NSColor.hermesRed : low ? NSColor.hermesOrange : menuPrimaryColor()) : menuTertiaryColor()
        view.addSubview(label(window.label, frame: NSRect(x: 28, y: 40, width: 170, height: 18), font: .systemFont(ofSize: 12, weight: .medium)))
        let displayValue: String
        if let amount = window.remainingAmount {
            displayValue = "\(formattedAmount(amount, currency: window.currency)) available"
        } else if let percent = window.remainingPercent {
            displayValue = window.limitReached ? "Limit reached" : "\(Int(percent.rounded()))% left"
        } else {
            displayValue = "Unavailable"
        }
        view.addSubview(label(displayValue, frame: NSRect(x: 190, y: 39, width: 150, height: 19), font: .monospacedDigitSystemFont(ofSize: 12, weight: .semibold), color: valueColor, alignment: .right))

        if window.limitReached {
            // OUT OF LIMIT: a 0%-full bar carries no information, so drop it. Give
            // the freed space to what actually matters when a window is blocked —
            // a PRECISE reset countdown (down to minutes, not the coarse "in 4h"),
            // and, unless this IS the weekly window, how much WEEKLY quota you still
            // have, since that's the real ceiling while this window is spent.
            let resetLine: String
            if let precise = preciseCountdown(window.resetsAt), let date = parsedDate(window.resetsAt) {
                resetLine = "Resets in \(precise) · \(date.formatted(date: .abbreviated, time: .shortened))"
            } else {
                resetLine = "No reset time reported"
            }
            view.addSubview(label(resetLine, frame: NSRect(x: 28, y: 22, width: 312, height: 16), font: .systemFont(ofSize: 11, weight: .medium), color: menuPrimaryColor()))
            if let weekly = weeklyWindow(provider), weekly.label != window.label, let pct = weekly.remainingPercent {
                let resetSuffix = preciseCountdown(weekly.resetsAt).map { " · resets in \($0)" } ?? ""
                let weeklyColor: NSColor = weekly.limitReached ? .hermesRed : (pct <= 15 ? .hermesOrange : menuSecondaryColor())
                view.addSubview(label("Weekly: \(Int(pct.rounded()))% left\(resetSuffix)", frame: NSRect(x: 28, y: 4, width: 312, height: 16), font: .systemFont(ofSize: 10.5, weight: .medium), color: weeklyColor))
            }
            return view
        }

        if window.remainingPercent != nil {
            let track = NSView(frame: NSRect(x: 28, y: 26, width: 312, height: 6))
            track.wantsLayer = true
            track.layer?.cornerRadius = 3
            track.layer?.backgroundColor = NSColor.separatorColor.withAlphaComponent(0.28).cgColor
            let fillWidth = 312 * max(0, min(100, value)) / 100
            let fill = NSView(frame: NSRect(x: 0, y: 0, width: fillWidth, height: 6))
            fill.wantsLayer = true
            fill.layer?.cornerRadius = 3
            fill.layer?.backgroundColor = bar.cgColor
            track.addSubview(fill)
            view.addSubview(track)
        }
        let subtitle: String
        if let reset = formattedReset(window.resetsAt) {
            subtitle = "Resets \(reset)"
        } else if let detail = window.detail {
            subtitle = detail
        } else if window.remainingAmount != nil {
            subtitle = "Available to spend"
        } else {
            subtitle = "No reset time reported"
        }
        view.addSubview(label(subtitle, frame: NSRect(x: 28, y: 4, width: 312, height: 17), font: .systemFont(ofSize: 10.5), color: menuSecondaryColor()))
        return view
    }

    // A precise "2h 15m" / "4d 3h" / "12m" countdown to a reset time — more
    // detailed than RelativeDateTimeFormatter's coarse "in 2 hours". Nil when the
    // time is missing, unparseable, or already past.
    private func preciseCountdown(_ resetsAt: String?) -> String? {
        guard let date = parsedDate(resetsAt) else { return nil }
        let secs = Int(date.timeIntervalSinceNow)
        guard secs > 0 else { return nil }
        let days = secs / 86400, hours = (secs % 86400) / 3600, mins = (secs % 3600) / 60
        if days > 0 { return "\(days)d \(hours)h" }
        if hours > 0 { return "\(hours)h \(mins)m" }
        return "\(max(1, mins))m"
    }

    // The provider's weekly window, if it reports one (label mentions "week").
    private func weeklyWindow(_ provider: QuotaProvider) -> QuotaWindow? {
        provider.windows.first { $0.label.lowercased().contains("week") }
    }

    private func detailView(_ detail: String) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 25))
        let image = NSImageView(frame: NSRect(x: 28, y: 6, width: 12, height: 12))
        image.image = NSImage(systemSymbolName: "creditcard", accessibilityDescription: nil)
        image.contentTintColor = menuTertiaryColor()
        view.addSubview(image)
        view.addSubview(label(detail, frame: NSRect(x: 47, y: 4, width: 293, height: 17), font: .systemFont(ofSize: 10.5), color: menuSecondaryColor()))
        return view
    }

    private func messageView(_ message: String, color: NSColor? = nil) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 30))
        view.addSubview(label(message, frame: NSRect(x: 28, y: 6, width: 312, height: 18), font: .systemFont(ofSize: 11), color: color ?? menuSecondaryColor()))
        return view
    }

    // Shown in place of a disconnected source's provider rows: no providers, no
    // quota — just a "sign in to fix" prompt with the login button right here in
    // the provider window (not only the small header icon).
    private func disconnectedPromptView(_ source: SourceQuota) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 44))
        let icon = NSImageView(frame: NSRect(x: 30, y: 14, width: 16, height: 16))
        icon.image = NSImage(systemSymbolName: "person.crop.circle.badge.exclamationmark", accessibilityDescription: nil)
        icon.contentTintColor = .hermesRed
        view.addSubview(icon)
        let msg = source.kind == .hermes
            ? "Not signed in — sign in to see quotas"
            : "Sign in to your local providers to see quotas"
        view.addSubview(label(msg, frame: NSRect(x: 54, y: 15, width: 196, height: 15),
                              font: .systemFont(ofSize: 11), color: menuSecondaryColor()))
        if source.kind == .hermes {
            let btn = textActionButton("Sign in", action: #selector(signInGateway), glowColor: .hermesGreen)
            btn.frame = NSRect(x: 258, y: 9, width: 88, height: 26)
            view.addSubview(btn)
        }
        return view
    }

    private func textActionButton(_ title: String, action: Selector, glowColor: NSColor = .hermesBlue) -> HoverGlowButton {
        let button = HoverGlowButton(title: title, target: self, action: action)
        button.controlSize = .small
        button.font = .systemFont(ofSize: 11, weight: .medium)
        button.glowColor = glowColor
        return button
    }

    private func iconActionButton(_ image: NSImage?, label: String, action: Selector, glowColor: NSColor = .hermesBlue) -> HoverGlowButton {
        let button = HoverGlowButton(title: "", target: self, action: action)
        button.controlSize = .small
        button.image = image
        button.imagePosition = .imageOnly
        button.imageScaling = .scaleProportionallyDown
        button.toolTip = label
        button.setAccessibilityLabel(label)
        button.glowColor = glowColor
        return button
    }

    private static let sourceOrder: [GatewayKind] = [.hermes, .local]

    // The sources are configured by the user via a multi-select dropdown — Hermes
    // (the gateway) and Local (locally-authenticated providers), both OFF by
    // default. The "Sources" row is a disclosure: click it to expand a checkbox per
    // source (rendered by rebuildMenu as sourceOptionRow), so you can activate one
    // or BOTH and it STAYS OPEN while you toggle. No fallback — deselecting a source
    // drops its providers.
    private func gatewayRowView() -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 40))
        let enabled = enabledGateways()
        let icon = NSImageView(frame: NSRect(x: 16, y: 12, width: 16, height: 16))
        icon.image = NSImage(systemSymbolName: "network", accessibilityDescription: nil)
        icon.contentTintColor = enabled.isEmpty ? .tertiaryLabelColor : (anyConnected ? .hermesBlue : .hermesRed)
        view.addSubview(icon)

        view.addSubview(label("Sources", frame: NSRect(x: 42, y: 12, width: 90, height: 16),
                              font: .systemFont(ofSize: 12, weight: .medium)))

        // Right-aligned summary of what's active + a disclosure chevron. A plain
        // NSButton in the row (reliable — nested NSMenus don't work inside a
        // status-bar menu, which is why the old pull-down didn't respond).
        let active = Self.sourceOrder.filter { gatewayEnabled($0) }
        let summary = active.isEmpty ? "Select…" : active.map { sourceName($0) }.joined(separator: ", ")
        view.addSubview(label(summary, frame: NSRect(x: 176, y: 12, width: 150, height: 16),
                              font: .systemFont(ofSize: 12), color: active.isEmpty ? menuTertiaryColor() : menuSecondaryColor(),
                              alignment: .right))
        let chevron = NSImageView(frame: NSRect(x: 334, y: 13, width: 12, height: 14))
        chevron.image = NSImage(systemSymbolName: sourcesExpanded ? "chevron.up" : "chevron.down", accessibilityDescription: nil)
        chevron.contentTintColor = menuTertiaryColor()
        chevron.imageScaling = .scaleProportionallyDown
        view.addSubview(chevron)

        // Whole-row toggle for the disclosure.
        let button = NSButton(frame: view.bounds)
        button.isBordered = false
        button.title = ""
        button.target = self
        button.action = #selector(toggleSourcesExpanded)
        button.toolTip = sourcesExpanded ? "Hide sources" : "Choose which sources are active"
        view.addSubview(button)
        return view
    }

    @objc private func toggleSourcesExpanded() {
        sourcesExpanded.toggle()
        rebuildMenu()
        applyMenuWindowAppearance()
    }

    // One expanded row per source, styled as an ACTIVATION SECTION (not a checkbox):
    // a rounded chip that FILLS with the source's status colour when active and sits
    // as a plain outline when off. A leading status dot + a right-aligned state label
    // ("Active" / "Activating…" / "Not signed in" / "Off") make the state obvious.
    // The whole row toggles the source and the menu stays open, so you can activate
    // one or both.
    private func sourceOptionRow(_ kind: GatewayKind) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 34))
        let on = gatewayEnabled(kind)
        let loading = activating.contains(kind)
        let connected = sources.first { $0.kind == kind }?.connected ?? false
        // Accent = the state colour: blue while activating, green connected, red
        // signed-out; grey when the section is off.
        let accent: NSColor = !on ? .tertiaryLabelColor
            : loading ? .hermesBlue
            : connected ? .hermesGreen : .hermesRed

        let chip = NSView(frame: NSRect(x: 38, y: 4, width: 306, height: 26))
        chip.wantsLayer = true
        chip.layer?.cornerRadius = 7
        chip.layer?.backgroundColor = (on ? accent.withAlphaComponent(0.16) : NSColor.clear).cgColor
        chip.layer?.borderColor = (on ? accent.withAlphaComponent(0.55)
                                      : NSColor.separatorColor.withAlphaComponent(0.55)).cgColor
        chip.layer?.borderWidth = 1
        view.addSubview(chip)

        let dot = NSImageView(frame: NSRect(x: 50, y: 12, width: 10, height: 10))
        dot.image = NSImage(systemSymbolName: on ? "circle.fill" : "circle", accessibilityDescription: nil)
        dot.contentTintColor = on ? accent : menuTertiaryColor()
        dot.imageScaling = .scaleProportionallyDown
        view.addSubview(dot)

        view.addSubview(label(sourceName(kind), frame: NSRect(x: 68, y: 9, width: 150, height: 16),
                              font: .systemFont(ofSize: 12, weight: on ? .semibold : .regular),
                              color: on ? menuPrimaryColor() : menuSecondaryColor()))

        let state = !on ? "Off" : loading ? "Activating…" : connected ? "Active" : "Not signed in"
        view.addSubview(label(state, frame: NSRect(x: 196, y: 9, width: 140, height: 16),
                              font: .systemFont(ofSize: 10.5, weight: .medium),
                              color: on ? accent : menuTertiaryColor(), alignment: .right))

        let button = NSButton(frame: view.bounds)
        button.isBordered = false
        button.title = ""
        button.identifier = NSUserInterfaceItemIdentifier(kind.rawValue)
        button.target = self
        button.action = #selector(toggleGatewayEnabled(_:))
        button.toolTip = on ? "Deactivate \(sourceName(kind))" : "Activate \(sourceName(kind))"
        view.addSubview(button)
        return view
    }

    // App-only appearance override, independent of the rest of macOS. nil/"system"
    // follows the OS; "light"/"dark" force it. The app otherwise uses semantic
    // colours + native menu vibrancy, so forcing .aqua / .darkAqua makes the whole
    // menu (and pets) match that theme's colours.
    private var appAppearanceMode: String {
        get { UserDefaults.standard.string(forKey: "appAppearance") ?? "system" }
        set { UserDefaults.standard.set(newValue, forKey: "appAppearance") }
    }

    private func applyAppearance() {
        switch appAppearanceMode {
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        case "dark":  NSApp.appearance = NSAppearance(named: .darkAqua)
        default:      NSApp.appearance = nil   // follow the system
        }
    }

    // The NSAppearance the app theme forces (nil = follow the system). A status-bar
    // NSMenu renders in the SYSTEM appearance regardless of NSApp.appearance, so we
    // also (a) override the menu window's appearance as it opens and (b) paint each
    // row an opaque themed background — together they re-theme the dropdown.
    private func themedAppearance() -> NSAppearance? {
        switch appAppearanceMode {
        case "light": return NSAppearance(named: .aqua)
        case "dark":  return NSAppearance(named: .darkAqua)
        default:      return nil
        }
    }

    // Whether the menu is effectively rendering LIGHT (forced Light, or System while
    // macOS is light). The translucent light menu washes out the default tertiary/
    // secondary greys, so muted text roles darken in that case only.
    private var isLightTheme: Bool {
        switch appAppearanceMode {
        case "light": return true
        case "dark":  return false
        default:      return NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) != .darkAqua
        }
    }

    // Explicit text colours for a light theme, so readability never depends on
    // semantic-colour resolution (which could render light-on-light). Fixed dark
    // greys on the soft light background; native greys in dark.
    private func menuPrimaryColor() -> NSColor { isLightTheme ? NSColor(srgbRed: 0.11, green: 0.11, blue: 0.13, alpha: 1) : .labelColor }
    private func menuSecondaryColor() -> NSColor { isLightTheme ? NSColor(srgbRed: 0.26, green: 0.26, blue: 0.29, alpha: 1) : .secondaryLabelColor }
    private func menuTertiaryColor() -> NSColor { isLightTheme ? NSColor(srgbRed: 0.40, green: 0.40, blue: 0.43, alpha: 1) : .tertiaryLabelColor }

    // Push the forced theme onto the live menu window (shared by all item views) so
    // its border/margins match the repainted rows. Best-effort; async catches the
    // window once AppKit has created it for display.
    private func applyMenuWindowAppearance() {
        let appearance = themedAppearance()
        let assign: () -> Void = { [weak menu] in
            if let window = menu?.items.lazy.compactMap({ $0.view?.window }).first {
                window.appearance = appearance
            }
        }
        assign()
        DispatchQueue.main.async(execute: assign)
    }

    // Icon + tooltip reflect the CURRENT mode (and what a click switches to). The
    // cycle is System → Light → Dark → System, so "System" stays reachable.
    private func appearanceSymbolName() -> String {
        switch appAppearanceMode {
        case "light": return "sun.max.fill"
        case "dark":  return "moon.fill"
        default:      return "circle.lefthalf.filled"   // following the system
        }
    }

    private func appearanceTooltip() -> String {
        switch appAppearanceMode {
        case "light": return "Theme: Light — click for Dark"
        case "dark":  return "Theme: Dark — click for System"
        default:      return "Theme: System — click for Light"
        }
    }

    @objc private func cycleAppearance() {
        switch appAppearanceMode {
        case "light": appAppearanceMode = "dark"
        case "dark":  appAppearanceMode = "system"
        default:      appAppearanceMode = "light"
        }
        applyAppearance()
        updateStatusItem()
        rebuildMenu()
        applyMenuWindowAppearance()   // retheme the open dropdown live
        updateActivityPets()
    }

    private func actionBarView() -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 42))
        // Refresh is per-provider now (a refresh icon on each provider row), so the
        // bottom bar has an app-theme toggle, Check-for-Updates and Close.
        let theme = iconActionButton(NSImage(systemSymbolName: appearanceSymbolName(), accessibilityDescription: nil),
                                     label: appearanceTooltip(),
                                     action: #selector(cycleAppearance), glowColor: .hermesBlue)
        theme.frame = NSRect(x: 250, y: 7, width: 28, height: 28)
        view.addSubview(theme)

        let update = iconActionButton(NSImage(systemSymbolName: "arrow.triangle.2.circlepath", accessibilityDescription: nil),
                                      label: "Check for Updates (pull latest from the repo)",
                                      action: #selector(checkForUpdates), glowColor: .hermesBlue)
        update.frame = NSRect(x: 282, y: 7, width: 28, height: 28)
        view.addSubview(update)

        let close = iconActionButton(NSImage(systemSymbolName: "xmark", accessibilityDescription: nil), label: "Close Provider Quotas", action: #selector(quit), glowColor: .hermesRed)
        close.frame = NSRect(x: 314, y: 7, width: 28, height: 28)
        view.addSubview(close)
        return view
    }

    // Where this app was installed from (a git checkout of the repo), recorded by
    // install-menubar.sh so "Check for Updates" knows what to pull + rebuild.
    private func sourceRepoPath() -> String? {
        if let p = UserDefaults.standard.string(forKey: "sourceRepo"),
           FileManager.default.fileExists(atPath: p + "/update.sh") {
            return p
        }
        return nil
    }

    private static func git() -> String {
        for p in ["/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"] where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        return "/usr/bin/git"
    }

    private static func updateTarget(_ repo: String) -> String? {
        let remoteHead = run(git(), ["-C", repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
        let target = remoteHead.out.trimmingCharacters(in: .whitespacesAndNewlines)
        if remoteHead.code == 0, !target.isEmpty {
            return target
        }
        let main = run(git(), ["-C", repo, "rev-parse", "--verify", "origin/main"])
        return main.code == 0 ? "origin/main" : nil
    }

    @discardableResult
    private static func run(_ launch: String, _ args: [String], cwd: String? = nil) -> (code: Int32, out: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: launch)
        proc.arguments = args
        if let cwd { proc.currentDirectoryURL = URL(fileURLWithPath: cwd) }
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        do { try proc.run() } catch { return (-1, "\(error)") }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return (proc.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    // Check the repo this app was installed from for newer commits; if behind,
    // offer to pull the latest and reinstall (rebuilds + relaunches the app). The
    // update runs DETACHED so it survives the app being booted out mid-reinstall.
    // A Homebrew install has no git checkout — its update path is `brew upgrade`.
    private func homebrewInstalled() -> Bool {
        Bundle.main.bundlePath.contains("/Cellar/provider-quotas/")
    }

    private func brewExecutable() -> String? {
        ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"].first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    @objc private func checkForUpdates() {
        guard let repo = sourceRepoPath() else {
            // Homebrew install → `brew upgrade` (then restart the service).
            if homebrewInstalled(), let brew = brewExecutable() {
                Self.notify("Updating…", "brew upgrade provider-quotas")
                DispatchQueue.global(qos: .userInitiated).async {
                    let up = Self.run(brew, ["upgrade", "provider-quotas"])
                    if up.code == 0 { _ = Self.run(brew, ["services", "restart", "provider-quotas"]) }
                    DispatchQueue.main.async {
                        Self.notify(up.code == 0 ? "Updated" : "Update finished",
                                    up.out.isEmpty ? "brew upgrade provider-quotas" : String(up.out.suffix(300)))
                    }
                }
                return
            }
            Self.notify("Update unavailable", "Reinstall from a git clone (install-menubar.sh), or `brew upgrade provider-quotas`.")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let git = Self.git()
            let fetch = Self.run(git, ["-C", repo, "fetch", "--quiet", "origin"])
            if fetch.code != 0 {
                DispatchQueue.main.async { Self.notify("Update check failed", fetch.out.isEmpty ? "git fetch failed" : fetch.out) }
                return
            }
            guard let target = Self.updateTarget(repo) else {
                DispatchQueue.main.async { Self.notify("Update check failed", "The repository default branch could not be found.") }
                return
            }
            let ancestry = Self.run(git, ["-C", repo, "merge-base", "--is-ancestor", "HEAD", target])
            let behind: Int
            if ancestry.code == 0 {
                let revisionList = Self.run(git, ["-C", repo, "rev-list", "--count", "HEAD..\(target)"])
                guard revisionList.code == 0,
                      let count = Int(revisionList.out.trimmingCharacters(in: .whitespacesAndNewlines)) else {
                    DispatchQueue.main.async { Self.notify("Update check failed", revisionList.out.isEmpty ? "Could not compare repository versions." : revisionList.out) }
                    return
                }
                behind = count
            } else if Self.run(git, ["-C", repo, "merge-base", "--is-ancestor", target, "HEAD"]).code == 0 {
                behind = 0
            } else {
                DispatchQueue.main.async { Self.notify("Update unavailable", "The local checkout has changes that are not on \(target). Merge or switch branches before updating.") }
                return
            }
            DispatchQueue.main.async {
                if behind == 0 {
                    Self.notify("Up to date", "You're on the latest version.")
                    return
                }
                let alert = NSAlert()
                alert.messageText = "Update available"
                alert.informativeText = "\(behind) new commit\(behind == 1 ? "" : "s") on the repo. Pull the latest and reinstall now? The app will rebuild and relaunch."
                alert.addButton(withTitle: "Update")
                alert.addButton(withTitle: "Later")
                NSApp.activate(ignoringOtherApps: true)
                if alert.runModal() == .alertFirstButtonReturn {
                    // Detached: install-menubar.sh boots this app out and back in, so
                    // it must not be a child of this process.
                    Self.run("/bin/sh", ["-c", "nohup bash \(Self.shellQuote(repo))/update.sh \(Self.shellQuote(target)) >/tmp/provider-quotas-update.log 2>&1 &"])
                    Self.notify("Updating…", "Pulling the latest and reinstalling — the menu-bar app will relaunch shortly.")
                }
            }
        }
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private static func notify(_ title: String, _ text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    private func rebuildMenu() {
        // No separators / divider lines — the sections are seamless.
        menu.removeAllItems()
        addView(titleView())
        addView(gatewayRowView())
        if sourcesExpanded {
            for kind in Self.sourceOrder { addView(sourceOptionRow(kind)) }
        }
        let enabled = enabledGateways()
        if enabled.isEmpty {
            addView(messageView("Enable Hermes or Local above"))
        } else {
            // One block per enabled source, each led by its header — which also
            // carries that source's pet controls (paw + switch + On/Off), so pet
            // config lives right where the source's name + connection show. A source
            // that's still activating (no data yet) shows a spinner in its place.
            for kind in enabled {
                guard let source = sources.first(where: { $0.kind == kind }) else {
                    if activating.contains(kind) || refreshing {
                        addView(loadingRowView(name: sourceName(kind)))
                    }
                    continue
                }
                addView(sourceHeaderView(source))
                if source.connected {
                    // Shown providers first, then the hidden ones grouped UNDER
                    // them as short rows (name + eye + colour point only — see
                    // providerView), so hiding a provider tucks it below instead of
                    // leaving a gap in place.
                    let ordered = source.providers.filter { providerShownInMenuBar(source.kind, $0.provider) }
                        + source.providers.filter { !providerShownInMenuBar(source.kind, $0.provider) }
                    for provider in ordered {
                        addView(providerView(provider, kind: source.kind, connected: source.connected))
                        // Collapsed by default; expand to see per-window detail. A
                        // hidden provider is never expanded — its row isn't expandable.
                        guard providerShownInMenuBar(source.kind, provider.provider),
                              expandedProviders.contains(providerKey(source.kind, provider.provider)) else { continue }
                        if provider.windows.isEmpty {
                            addView(messageView(provider.message ?? provider.status.replacingOccurrences(of: "_", with: " "), color: .hermesOrange))
                        }
                        for window in provider.windows {
                            addView(windowView(window, provider: provider))
                        }
                        for detail in provider.details {
                            addView(detailView(detail))
                        }
                    }
                } else {
                    // Disconnected source: don't list individual providers or any
                    // quota — just prompt to sign in (the header already shows it's
                    // down). Avoids stale/leftover quota reading as if it's live.
                    addView(disconnectedPromptView(source))
                }
            }
        }
        addView(actionBarView())
    }

    // Shown in place of a source's block while it's activating (just switched on,
    // fetching its first quotas). Styled like the source's own SECTION HEADER — its
    // name uppercased with a spinner where the connection dot would be, plus a
    // right-aligned "Loading…" — so the loading reads as happening inside that
    // source's section (and the pet stays hidden until it's done).
    private func loadingRowView(name: String) -> NSView {
        let view = menuMaterialView(NSRect(x: 0, y: 0, width: 360, height: 28))
        let spinner = NSProgressIndicator(frame: NSRect(x: 15, y: 8, width: 12, height: 12))
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.usesThreadedAnimation = true   // keeps spinning while the menu tracks
        spinner.startAnimation(nil)
        view.addSubview(spinner)
        view.addSubview(label(name.uppercased(), frame: NSRect(x: 30, y: 8, width: 150, height: 14),
                              font: .systemFont(ofSize: 10, weight: .semibold), color: menuSecondaryColor()))
        view.addSubview(label("Loading…", frame: NSRect(x: 190, y: 8, width: 154, height: 14),
                              font: .systemFont(ofSize: 10.5), color: menuTertiaryColor(), alignment: .right))
        return view
    }

    private func parsedDate(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    }

    private func formattedRelativeDate(_ value: String?) -> String? {
        guard let date = parsedDate(value) else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    private func formattedReset(_ value: String?) -> String? {
        guard let date = parsedDate(value) else { return nil }
        let relative = RelativeDateTimeFormatter()
        relative.unitsStyle = .short
        let relativeText = relative.localizedString(for: date, relativeTo: Date())
        return "\(relativeText) · \(date.formatted(date: .abbreviated, time: .shortened))"
    }

    // Per-source Refresh (icon in the source header): re-check that whole source.
    // Its header shows a spinner meanwhile.
    @objc private func refreshSourceAction(_ sender: NSButton) {
        guard let raw = sender.identifier?.rawValue, let kind = GatewayKind(rawValue: raw) else { return }
        guard !refreshingKinds.contains(kind) else { return }
        refreshingKinds.insert(kind)
        rebuildMenu()
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = Self.fetchPayload(kind: kind)
            DispatchQueue.main.async {
                guard let self else { return }
                self.refreshingKinds.remove(kind)
                switch result {
                case .success(let payload):
                    let providers = self.applyLastGood(payload.providers, kind: kind)
                    self.upsertSource(SourceQuota(kind: kind, providers: providers, connected: true, generatedAt: payload.generatedAt))
                    self.lastProvidersByKind[kind.rawValue] = providers.map { ($0.provider, $0.label) }
                    UserDefaults.standard.set(providers.map { $0.provider }, forKey: "lastProviderSlugs.\(kind.rawValue)")
                    UserDefaults.standard.set(providers.map { $0.label }, forKey: "lastProviderLabels.\(kind.rawValue)")
                case .failure:
                    self.upsertSource(SourceQuota(kind: kind, providers: self.disconnectedProviders(for: kind), connected: false, generatedAt: nil))
                }
                self.noteQuotaTransitions()   // catch any provider that just reset
                self.updateStatusItem()
                self.rebuildMenu()
                self.updateActivityPets()
            }
        }
    }

    // Replace (or insert) one source, keeping the Hermes-before-Local order.
    private func upsertSource(_ source: SourceQuota) {
        // Drop a result for a source disabled while its fetch was in flight — it
        // must not re-add that source's dots after the user turned it off.
        guard gatewayEnabled(source.kind) else {
            sources.removeAll { $0.kind == source.kind }
            return
        }
        if let index = sources.firstIndex(where: { $0.kind == source.kind }) {
            sources[index] = source
        } else {
            sources.append(source)
            sources.sort { ($0.kind == .hermes ? 0 : 1) < ($1.kind == .hermes ? 0 : 1) }
        }
    }

    // Cycle ONE source's pet to the next installed one.
    @objc private func cycleSourcePet(_ sender: NSButton) {
        guard let key = sender.identifier?.rawValue else { return }
        let pets = PetCatalog.installed()
        guard pets.count > 1 else { return }
        let currentId = PetCatalog.selected(forKey: key)?.id
        let index = pets.firstIndex { $0.id == currentId } ?? -1
        PetCatalog.setSelected(pets[(index + 1) % pets.count].id, forKey: key)
        updateActivityPets()
        rebuildMenu()
    }

    // Activate / deactivate the pet for ONE source.
    @objc private func toggleSourcePet(_ sender: NSButton) {
        guard let key = sender.identifier?.rawValue else { return }
        PetCatalog.setPetEnabled(!PetCatalog.petEnabled(forKey: key), forKey: key)
        if key == GatewayKind.hermes.rawValue { pollHermesActivity() }
        updateActivityPets()
        rebuildMenu()
    }

    // Enable / disable a single source. sender.identifier is the kind's rawValue.
    @objc private func toggleGatewayEnabled(_ sender: NSButton) {
        guard let raw = sender.identifier?.rawValue, let kind = GatewayKind(rawValue: raw) else { return }
        toggleGateway(kind)
    }

    private func toggleGateway(_ kind: GatewayKind) {
        let nowOn = !gatewayEnabled(kind)
        setGatewayEnabled(kind, nowOn)
        if nowOn {
            // Turning a source on kicks off a fetch — mark it activating so the
            // menu shows a spinner for it until the data lands.
            activating.insert(kind)
        } else {
            activating.remove(kind)
            // Disabling a source removes ALL of its providers: drop it from the
            // live list AND its cached copy so nothing linked to it lingers.
            sources.removeAll { $0.kind == kind }
            lastProvidersByKind[kind.rawValue] = nil
            UserDefaults.standard.removeObject(forKey: "lastProviderSlugs.\(kind.rawValue)")
            UserDefaults.standard.removeObject(forKey: "lastProviderLabels.\(kind.rawValue)")
        }
        reloadAfterGatewayChange()
    }

    private func reloadAfterGatewayChange() {
        updateStatusItem()
        rebuildMenu()
        refresh()
        updateActivityPets()
        pollHermesActivity()
    }

    // Pull pets that were downloaded on the gateway (remote Desktop installs them
    // there, not on this Mac) into the local pets-cache so they appear in the
    // picker. Uses the same authenticated helper as the quota fetch.
    private func syncPetsFromGateway() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let fm = FileManager.default
            let helper = Self.helperPath("hermes-desktop-quotas")
            guard fm.isExecutableFile(atPath: helper) else { return }
            guard let listData = Self.runHelper(helper, ["/api/plugins/provider-quota/pets"]),
                  let obj = try? JSONSerialization.jsonObject(with: listData) as? [String: Any],
                  let pets = obj["pets"] as? [[String: Any]] else { return }
            let localIds = Set(PetCatalog.installed().map { $0.id })
            var changed = false
            for pet in pets {
                guard let id = pet["id"] as? String, !localIds.contains(id), !PetCatalog.isHidden(id) else { continue }
                let dir = PetCatalog.cacheDir.appendingPathComponent(id)
                let sheet = dir.appendingPathComponent("spritesheet.webp")
                if fm.fileExists(atPath: sheet.path) { continue }
                try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
                _ = Self.runHelper(helper, ["/api/plugins/provider-quota/pets/\(id)/spritesheet", sheet.path])
                guard let data = try? Data(contentsOf: sheet), !data.isEmpty else {
                    try? fm.removeItem(at: dir); continue
                }
                let meta: [String: Any] = [
                    "id": id,
                    "displayName": pet["displayName"] as? String ?? id,
                    "spritesheetPath": "spritesheet.webp",
                ]
                if let md = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted]) {
                    try? md.write(to: dir.appendingPathComponent("pet.json"))
                }
                changed = true
            }
            // Prune cached pets that were deleted on the gateway (we only reach
            // here on a SUCCESSFUL list fetch, so an unreachable gateway never
            // wipes the cache). Local ~/.hermes/pets are left untouched.
            let gatewayIds = Set(pets.compactMap { $0["id"] as? String })
            if let cached = try? fm.contentsOfDirectory(at: PetCatalog.cacheDir, includingPropertiesForKeys: [.isDirectoryKey]) {
                for dir in cached where ((try? dir.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory ?? false)
                    && !gatewayIds.contains(dir.lastPathComponent) {
                    try? fm.removeItem(at: dir)
                    changed = true
                }
            }
            if changed {
                DispatchQueue.main.async {
                    self?.updateActivityPets()
                    self?.rebuildMenu()
                }
            }
        }
    }

    private static func runHelper(_ path: String, _ args: [String]) -> Data? {
        let proc = Process()
        // Run via a login shell so the helper's `#!/usr/bin/env bash` shebang and
        // its tools resolve regardless of the launch PATH (same as the quota path).
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = ["-lc", "exec \"$0\" \"$@\"", path] + args
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        do { try proc.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return proc.terminationStatus == 0 ? data : nil
    }

    // Rebuild the floating pets: ONE pet per enabled source (Hermes / Local) whose
    // pet is switched on, each carrying that source's own chosen art. A source's
    // pet becomes ACTIVE (per session) when the source is being used, sits idle
    // otherwise, or shows a "not working" state when the source is down. With no
    // source pet on, the panel hides. Polled on a short timer plus on every
    // refresh / toggle.
    private func updateActivityPets() {
        // Local usage needs a `ps`/transcript scan (blocking) — do it off-main and
        // render on main. Hermes usage comes from a cached gateway-status poll (see
        // pollHermesActivity), so there's no network call in this hot path.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let local = Self.fetchLocalActivity()
            DispatchQueue.main.async {
                guard let self else { return }
                self.lastLocalSessions = local
                self.activityPanel.show(self.buildSourcePetTiles(localSessions: local))
                self.refreshStatusDotsIfActiveChanged()
            }
        }
    }

    // Redraw the menu-bar dots only when the set of actively-running providers
    // changes, so their session glow stays live (~1s) without a needless redraw
    // every tick.
    private var lastActiveSignature = ""
    private func refreshStatusDotsIfActiveChanged() {
        let sig = activeSessionColors(.hermes).union(activeSessionColors(.local)).sorted().joined(separator: ",")
        if sig != lastActiveSignature {
            lastActiveSignature = sig
            updateStatusItem()
        }
    }

    private func buildSourcePetTiles(localSessions: [LocalSession]) -> [PetTile] {
        let petSources = enabledGateways().filter { PetCatalog.petEnabled(forKey: $0.rawValue) }
        if petSources.isEmpty { return [] }

        // ONE pet per source. It shows a dot ONLY for each session that's actively
        // RUNNING, in that session's provider colour. When a session finishes its
        // dot simply disappears — no lingering checkmark — and when nothing is
        // running there are no dots at all.
        var tiles: [PetTile] = []
        for kind in petSources {
            let pet = PetCatalog.selected(forKey: kind.rawValue)
            // While a source is still ACTIVATING (just switched on, fetching its
            // first data) show NO pet at all — not even the error pet. The pet only
            // appears once loading is DONE: the idle/working pet if it connected, or
            // the error pet if it came back disconnected (e.g. Hermes Desktop not
            // signed in).
            if activating.contains(kind) { continue }
            let connected = sources.first { $0.kind == kind }?.connected ?? false
            if !connected {
                tiles.append(PetTile(instance: Self.needsLoginPlaceholder(gateway: sourceName(kind)), pet: pet, sourceKey: kind.rawValue))
                continue
            }

            // Providers the user hid via the eye toggle, and providers that are OUT
            // OF QUOTA or disconnected, must not light a running dot. A hidden
            // provider still shows an ATTENTION dot (so you never miss a prompt); an
            // out-of-quota / disconnected provider shows NOTHING — an exhausted or
            // offline provider has no live session worth a point.
            let sourceProviders = sources.first { $0.kind == kind }?.providers ?? []
            let hiddenColors = Set(sourceProviders
                .filter { !providerShownInMenuBar(kind, $0.provider) }
                .map { Self.providerHex($0.provider).lowercased() })
            let exhaustedColors = Set(sourceProviders
                .filter { providerRingColor($0, connected: connected) != nil }
                .map { Self.providerHex($0.provider).lowercased() })
            func visibleMark(_ hex: String, needsInput: Bool = false, needsPermission: Bool = false) -> SessionMark? {
                let attention = needsInput || needsPermission
                let busy = !attention   // waiting sessions aren't generating
                // A session that NEEDS YOU (input/permission) ALWAYS shows — even for a
                // hidden or out-of-quota provider — so you never miss a prompt; it's a
                // live session awaiting you, not idle work. Only RUNNING dots are
                // filtered: drop a colour that's out of quota / disconnected (no real
                // work) or hidden via the eye.
                guard !attention else {
                    return SessionMark(hex: hex, busy: busy, needsInput: needsInput, needsPermission: needsPermission)
                }
                let kept = hex.split(separator: ",").map(String.init)
                    .filter { !exhaustedColors.contains($0.lowercased()) && !hiddenColors.contains($0.lowercased()) }
                return kept.isEmpty ? nil : SessionMark(hex: kept.joined(separator: ","), busy: busy, needsInput: needsInput, needsPermission: needsPermission)
            }

            var marks: [SessionMark]
            let title: String
            let working: Bool
            if kind == .hermes {
                // Running sessions light a busy dot; sessions the gateway flagged as
                // waiting get a dim dot with an attention badge — an input badge or a
                // permission (lock) badge (see drawPet).
                let running = hermesSessionHexes.compactMap { visibleMark($0) }
                let waiting = hermesWaitingHexes.compactMap { visibleMark($0, needsInput: true) }
                let permission = hermesPermissionHexes.compactMap { visibleMark($0, needsPermission: true) }
                marks = running + waiting + permission
                title = running.isEmpty ? "" : "\(running.count) agent\(running.count == 1 ? "" : "s") running"
                working = hermesBusy || !running.isEmpty
            } else {
                // Local has no gateway to report a waiting-for-input state, so its
                // sessions are running (busy) or nothing.
                let busy = localSessions.filter { $0.busy }   // idle/open sessions show nothing
                marks = busy.compactMap { visibleMark($0.hex) }
                title = "\(marks.count) session\(marks.count == 1 ? "" : "s")"
                working = !marks.isEmpty
            }

            // A session waiting for you (input OR permission) makes the pet WAVE for
            // attention (the non-error "waiting" animation), taking priority over the
            // plain working/idle state. Otherwise it moves while working and sits idle.
            let needsPermission = marks.contains { $0.needsPermission }
            let needsAttention = needsPermission || marks.contains { $0.needsInput }
            let recovered = recoveredProviderLabels(kind)
            let display: ProviderActivityInstance
            if needsAttention {
                let n = marks.filter { $0.needsInput || $0.needsPermission }.count
                let noun = needsPermission ? "permission" : "input"
                display = Self.waitingSourceInstance(name: sourceName(kind),
                    title: "\(n) session\(n == 1 ? "" : "s") need\(n == 1 ? "s" : "") your \(noun)")
            } else if !recovered.isEmpty {
                // A provider just reset — WAVE to celebrate that quota's back.
                let names = recovered.joined(separator: ", ")
                display = Self.celebrateSourceInstance(name: sourceName(kind),
                    title: "\(names) quota back")
            } else if working {
                display = Self.workingSourceInstance(name: sourceName(kind), title: title)
            } else {
                display = Self.idleSourceInstance(name: sourceName(kind))
            }
            tiles.append(PetTile(instance: display, pet: pet, sessionCount: max(marks.count, 1), sessions: marks, sourceKey: kind.rawValue))
        }
        return tiles
    }

    // Whether the Hermes gateway is working right now, plus one provider colour
    // PER running session (so N concurrent sessions → N dots, even several of the
    // same provider). We read /api/status for the aggregate flags and then scan
    // every profile's recent sessions, emitting a colour for each that's live
    // (`is_active`, or still-open and just touched), keyed off its
    // `billing_provider` — so OpenRouter shows purple, Codex green, etc. Works for
    // a REMOTE gateway, where sessions run server-side.
    private static func fetchGatewayActivity() -> (busy: Bool, agents: Int, sessionHexes: [String], waitingHexes: [String], permissionHexes: [String]) {
        let helper = helperPath("hermes-desktop-quotas")
        guard FileManager.default.isExecutableFile(atPath: helper) else {
            return (false, 0, [], [], [])
        }
        // One helper call resolves the gateway once and returns /api/status plus
        // every profile's recent sessions (see desktop-quotas.sh --activity). We
        // scan those sessions UNCONDITIONALLY, because bot turns run as `source:
        // cli` sessions that never flip the aggregate active_agents/active_sessions
        // counters — so a Codex-backed bot mid-reply looks idle in /api/status but
        // is plainly running in its session's flags.
        var agents = 0, statusBusy = false
        var hexes: [String] = []
        var waitingHexes: [String] = []
        var permissionHexes: [String] = []
        if let data = runHelper(helper, ["--activity"]),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            agents = (obj["agents"] as? Int) ?? 0
            statusBusy = (obj["status_busy"] as? Bool) ?? false
            let now = Date().timeIntervalSince1970
            for session in (obj["sessions"] as? [[String: Any]]) ?? [] {
                // The gateway can flag a session as WAITING FOR YOU — a PERMISSION /
                // approval prompt (priority) or a plain INPUT prompt. Either gets an
                // attention dot + badge regardless of is_active (it isn't generating),
                // so route it out before the "is it running?" gate below.
                if (session["needs_permission"] as? Bool) ?? false {
                    permissionHexes.append(Self.sessionHexes(for: session))
                    continue
                }
                if (session["needs_input"] as? Bool) ?? false {
                    waitingHexes.append(Self.sessionHexes(for: session))
                    continue
                }
                // A session lights a dot while it's genuinely RUNNING.
                //
                // CLOSED (ended_at set) → DONE: show only for a short grace so an
                // ephemeral turn (OpenRouter/CLI close the session the instant it
                // ends) still registers, then clear promptly.
                //
                // OPEN (ended_at null) → live ONLY while the gateway marks it
                // is_active. Verified: is_active flips False the instant a turn
                // COMPLETES or is STOPPED, so gating on it clears a finished/stopped
                // session that stays open (ended_at null) — the "pet didn't update
                // after stop/complete" bug (it used to linger the full freshness
                // window). last_active is only a BACKSTOP that reaps a zombie
                // (killed mid-turn: is_active stuck True, last_active frozen). We do
                // NOT use a tight last_active window for open sessions — the gateway
                // may not bump last_active during a long generation, so is_active
                // carries liveness while last_active only bounds zombies. If a
                // gateway omits is_active, fall back to plain freshness.
                let endedRaw = session["ended_at"]
                let endedAt: Double? = (endedRaw is NSNull) ? nil : endedRaw as? Double
                if let endedAt {
                    guard now - endedAt < hermesEndedGrace else { continue }
                } else {
                    let lastActive = session["last_active"] as? Double
                    if let isActive = session["is_active"] as? Bool {
                        guard isActive, let lastActive, now - lastActive < hermesZombieWindow else { continue }
                    } else {
                        guard let lastActive, now - lastActive < hermesSessionFreshWindow else { continue }
                    }
                }
                // Colour by the SESSION's provider(s) — one (possibly multi-colour)
                // dot per running session (see sessionHexes(for:)).
                hexes.append(Self.sessionHexes(for: session))
            }
        }
        let busy = statusBusy || !hexes.isEmpty
        return (busy, agents, hexes, waitingHexes, permissionHexes)
    }

    // One (possibly multi-colour, comma-joined) hex string for a gateway session —
    // a colour per DISTINCT model family it runs (mixture-of-agents), else its
    // single resolved provider colour.
    private static func sessionHexes(for session: [String: Any]) -> String {
        let families = (session["providers"] as? [String])?.filter { !$0.isEmpty } ?? []
        if !families.isEmpty {
            var seen = Set<String>()
            var hs: [String] = []
            for f in families {
                let h = sessionHex(billing: f, provider: nil, model: nil)
                if seen.insert(h).inserted { hs.append(h) }
            }
            return hs.joined(separator: ",")
        }
        return sessionHex(billing: session["billing_provider"] as? String,
                          provider: session["provider"] as? String,
                          model: session["model"] as? String)
    }

    // Provider colour for one gateway session. The --activity helper pre-resolves
    // the provider from the model family and sends it as `billing`; map it here.
    // If a raw `model` is present instead, resolve it the same way: OpenRouter
    // serves "vendor/model" slugs (contain "/"); Codex models are gpt-*/o#/codex;
    // Claude models are claude-*. Fall back to billing, then provider, then grey.
    static func sessionHex(billing: String?, provider: String?, model: String?) -> String {
        if let m = model?.lowercased(), !m.isEmpty {
            if m.contains("/") { return providerHex("openrouter") }
            if m.hasPrefix("claude") { return providerHex("anthropic") }
            if m.hasPrefix("gpt") || m.contains("codex") || m.hasPrefix("o1") || m.hasPrefix("o3") || m.hasPrefix("o4") {
                return providerHex("openai-codex")
            }
        }
        if let b = billing, !b.isEmpty { return providerHex(b) }
        if let p = provider, !p.isEmpty { return providerHex(p) }
        return providerHex("")   // neutral grey
    }

    // Poll the gateway for Hermes activity and cache it; refresh the pets when it
    // changes. Called on a network timer, separate from the 1s local scan. The
    // in-flight guard keeps a slow round-trip (it makes several remote calls) from
    // piling up behind the 2s timer.
    private func pollHermesActivity() {
        guard gatewayEnabled(.hermes), PetCatalog.petEnabled(forKey: "hermes") else {
            if hermesBusy || hermesActiveAgents != 0 {
                hermesBusy = false; hermesActiveAgents = 0; updateActivityPets()
            }
            return
        }
        guard !hermesPollInFlight else { return }
        hermesPollInFlight = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let (busy, agents, hexes, waiting, permission) = Self.fetchGatewayActivity()
            DispatchQueue.main.async {
                guard let self else { return }
                self.hermesPollInFlight = false
                // Short bot/helper turns live only a few seconds — with a 2s poll
                // timer plus a multi-request helper round-trip, a dot that clears
                // the instant the turn ends is on screen for a single tick and
                // reads as "never showed". HOLD the last-seen running dots for a
                // short, bounded grace period after they disappear, so brief turns
                // register while long-finished ones still clear automatically.
                var effectiveHexes = hexes
                var effectiveBusy = busy
                if hexes.isEmpty,
                   let until = self.hermesDotHoldUntil, Date() < until,
                   !self.hermesHeldHexes.isEmpty {
                    effectiveHexes = self.hermesHeldHexes
                    effectiveBusy = true
                }
                if !hexes.isEmpty {
                    self.hermesDotHoldUntil = Date().addingTimeInterval(Self.hermesDotHoldSeconds)
                    self.hermesHeldHexes = hexes
                }
                let changed = self.hermesBusy != effectiveBusy || self.hermesActiveAgents != agents
                    || self.hermesSessionHexes != effectiveHexes || self.hermesWaitingHexes != waiting
                    || self.hermesPermissionHexes != permission
                self.hermesBusy = effectiveBusy
                self.hermesActiveAgents = agents
                self.hermesSessionHexes = effectiveHexes
                self.hermesWaitingHexes = waiting
                self.hermesPermissionHexes = permission
                if changed { self.updateActivityPets() }
            }
        }
    }

    // Grace period that keeps the Hermes pet's last-seen session dots lit after a
    // turn ends — smooths a single empty poll tick. Kept SMALL (the hermesEndedGrace
    // window already keeps ephemeral turns visible); a big hold was the "pet keeps
    // going after the turn ends" lag.
    private static let hermesDotHoldSeconds: TimeInterval = 2
    // How stale an OPEN session's last write may be while still counting as
    // running. A live turn heartbeats every <=30s, so 75s spans two missed beats;
    // open-but-idle sessions age out of the dots within about a minute.
    private static let hermesSessionFreshWindow: TimeInterval = 75
    // How long a CLOSED session (ended_at set) still lights a dot after it ends —
    // long enough that an ephemeral turn registers and survives a poll/round-trip,
    // short enough that the pet stops promptly when a turn completes.
    private static let hermesEndedGrace: TimeInterval = 6
    // Backstop for an OPEN session that's still is_active but whose last_active has
    // frozen. is_active is the gateway's authoritative liveness flag (True while a
    // turn runs, False the instant it completes/stops), and the gateway does NOT
    // bump last_active during a long Desktop generation — so we must TRUST
    // is_active and NOT clear a still-active session on last_active age (doing so
    // blinked live Desktop turns off after ~2min). This window only reaps a
    // pathological zombie (is_active stuck True after a hard crash); kept large so a
    // genuinely long turn is never dropped. The gateway also reaps stale is_active
    // itself (~300s), so this rarely bites.
    private static let hermesZombieWindow: TimeInterval = 900
    private var hermesDotHoldUntil: Date?
    private var hermesHeldHexes: [String] = []

    private static func workingSourceInstance(name: String, title: String) -> ProviderActivityInstance {
        ProviderActivityInstance(
            completedAt: nil, key: "\(name):working", model: "", profile: name,
            provider: "source", providerColor: "#0053fd", providerLabel: "",
            sessionId: "", startedAt: Date().timeIntervalSince1970,
            status: "working", title: title)
    }

    // A source with a session WAITING FOR YOUR INPUT: the non-error "waiting"
    // animation (drawNukey row 6 — a wave for attention) + orange halo, distinct
    // from the red "failed" error state. Not an error, just "come look at me".
    private static func waitingSourceInstance(name: String, title: String) -> ProviderActivityInstance {
        ProviderActivityInstance(
            completedAt: nil, key: "\(name):waiting", model: "", profile: name,
            provider: "source", providerColor: "#db704b", providerLabel: "",
            sessionId: "", startedAt: Date().timeIntervalSince1970,
            status: "waiting", title: title)
    }

    // A source whose provider just got its quota back (window reset): a happy WAVE
    // (drawNukey row 3) with a GREEN halo — "you're good to go again" — distinct
    // from the orange input-wait wave and the red failed state.
    private static func celebrateSourceInstance(name: String, title: String) -> ProviderActivityInstance {
        ProviderActivityInstance(
            completedAt: nil, key: "\(name):recovered", model: "", profile: name,
            provider: "source", providerColor: "#43c46b", providerLabel: "",
            sessionId: "", startedAt: Date().timeIntervalSince1970,
            status: "recovered", title: title)
    }

    // Seconds since a session's log was last written before we treat it as idle.
    private static let localBusyWindow = 20.0     // log written this recently ⇒ actively processing
    private static let localPresentWindow = 180.0 // …this recently ⇒ session still open (idle)
    // Codex flushes its rollout in BURSTS — during model reasoning it can go
    // ~20–30s between writes — so a 20s window would blink the dot off mid-turn.
    // A wider window keeps a working Codex session lit through those gaps and
    // clears it a bit after the session truly goes quiet. (Claude streams its
    // transcript continuously, so it keeps the tighter localBusyWindow.)
    private static let codexBusyWindow = 90.0
    // opencode (OpenRouter) writes its SQLite store in bursts too: during model
    // reasoning it can go a minute+ between writes — measured gaps up to ~90s
    // mid-turn — so the 20s window blinked the dot off through every reasoning
    // pause (the "OpenRouter session shows no dot in the pet" report). A wider
    // window keeps a working opencode/OpenRouter session lit through those gaps
    // and clears it a bit after the turn truly ends.
    private static let opencodeBusyWindow = 120.0

    struct LocalSession { let key: String; let hex: String; let busy: Bool; let order: Int }

    // Local sessions currently RUNNING, across providers — a Claude Code session
    // (its `claude` process is alive), a Codex session (its rollout was written
    // recently), or an opencode session (its DB row was touched recently).
    // `busy` = actively processing right now (its log is fresh within
    // that tool's write cadence — see localBusyWindow / codexBusyWindow);
    // otherwise it's running-but-idle. Grouped Claude, then Codex, then opencode.
    private static func fetchLocalActivity() -> [LocalSession] {
        let now = Date()
        return (claudeSessions(now: now) + codexSessions(now: now) + opencodeSessions(now: now)).sorted { $0.order < $1.order }
    }

    // Assistant `stop_reason` values that mean the turn is OVER (not waiting on a
    // tool). When the last message event is one of these, the session is idle.
    private static let claudeTerminalStops: Set<String> = ["end_turn", "stop_sequence", "max_tokens"]

    // Is a Claude session actively working, read from its transcript's tail so the
    // dot clears the instant a turn ends — no window/lag. The last message event
    // tells us the exact state: a `user` line means a prompt or tool-result is
    // awaiting a response (working); an `assistant` line with a terminal
    // stop_reason means the turn finished (idle); anything else (a `tool_use`
    // stop, or a still-forming message) means working. Meta lines (title, mode,
    // pr-link, …) are skipped. Returns nil if the tail can't be decided, so the
    // caller can fall back to file freshness.
    private static func claudeTurnActive(_ url: URL) -> Bool? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        let tail: UInt64 = 512 * 1024
        let start = size > tail ? size - tail : 0
        guard (try? handle.seek(toOffset: start)) != nil,
              let data = try? handle.readToEnd(), !data.isEmpty else { return nil }
        var lines = String(decoding: data, as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
        if start > 0, !lines.isEmpty { lines.removeFirst() }   // drop the partial first line
        for line in lines.reversed() {
            guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  let type = obj["type"] as? String else { continue }
            if type == "user" { return true }          // prompt / tool-result → a reply is due
            if type == "assistant" {
                let stop = (obj["message"] as? [String: Any])?["stop_reason"] as? String
                if let stop, claudeTerminalStops.contains(stop) { return false }   // turn finished
                return true                              // tool_use pending or mid-generation
            }
            // system / summary / meta line → keep scanning backward
        }
        return nil
    }

    // Every live `claude` process = a present session; busy reflects its
    // transcript's exact turn state (see claudeTurnActive), falling back to file
    // freshness. The session id comes from the command line when present
    // (--session-id/--resume); a headless launch that carries neither — e.g.
    // Hermes Desktop runs `claude --output-format stream-json` — is resolved by
    // the transcript the live process holds OPEN (lsof), so those sessions light
    // the pet too instead of being invisible.
    private static func claudeSessions(now: Date) -> [LocalSession] {
        // pid + command per process, so we can both read session flags and lsof the
        // pids that carry none.
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/ps")
        proc.arguments = ["-axo", "pid=,command="]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return [] }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let text = String(data: data, encoding: .utf8) else { return [] }

        let nonSession: Set<String> = ["login", "logout", "mcp", "config", "doctor",
                                       "update", "upgrade", "install", "migrate-installer",
                                       "--version", "-v", "--help", "-h"]
        let projects = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/projects")
        let dirs = (try? FileManager.default.contentsOfDirectory(at: projects, includingPropertiesForKeys: nil)) ?? []
        // id -> its transcript (nil until resolved). Deduped by id so the same
        // session found via a flag AND via lsof isn't counted twice.
        var transcriptByID: [String: URL] = [:]
        var flagIDs = Set<String>()
        var headlessPIDs: [Int32] = []
        for raw in text.split(separator: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard let sp = line.firstIndex(of: " ") else { continue }
            let pid = Int32(line[..<sp]) ?? -1
            let command = line[line.index(after: sp)...]
            let tokens = command.split(separator: " ").map(String.init)
            guard let first = tokens.first,
                  (first as NSString).lastPathComponent == "claude" else { continue }
            if tokens.count > 1, nonSession.contains(tokens[1]) { continue }
            if let id = flagValue("--session-id", in: tokens) ?? flagValue("--resume", in: tokens) {
                flagIDs.insert(id)
            } else if pid > 0 {
                headlessPIDs.append(pid)
            }
        }
        // Flag-id sessions: locate their transcript by filename (existing behaviour).
        for id in flagIDs {
            for dir in dirs {
                let candidate = dir.appendingPathComponent("\(id).jsonl")
                if FileManager.default.fileExists(atPath: candidate.path) {
                    transcriptByID[id] = candidate
                    break
                }
            }
        }
        // Headless sessions: the id is the transcript the process holds open.
        for url in openClaudeTranscripts(pids: headlessPIDs) {
            let id = url.deletingPathExtension().lastPathComponent
            if transcriptByID[id] == nil { transcriptByID[id] = url }
        }
        // Include flag ids that resolved to no transcript too (a just-started session).
        let ids = Set(transcriptByID.keys).union(flagIDs)
        guard !ids.isEmpty else { return [] }

        let hex = providerHex("anthropic")
        return ids.map { id in
            let transcript = transcriptByID[id]
            var age = Double.greatestFiniteMagnitude
            if let transcript,
               let modified = (try? FileManager.default.attributesOfItem(atPath: transcript.path))?[.modificationDate] as? Date {
                age = now.timeIntervalSince(modified)
            }
            // Prefer the exact turn state (no lag); fall back to freshness only if
            // the transcript tail can't be parsed.
            let busy = transcript.flatMap { claudeTurnActive($0) } ?? (age < localBusyWindow)
            return LocalSession(key: "local:claude:\(id)", hex: hex, busy: busy, order: 0)
        }
    }

    // Transcripts (~/.claude/projects/**/<id>.jsonl) currently held OPEN by the
    // given pids — one lsof call resolves a headless `claude` session's id without
    // a command-line flag. Empty pids → no call.
    private static func openClaudeTranscripts(pids: [Int32]) -> [URL] {
        guard !pids.isEmpty else { return [] }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        proc.arguments = ["-nP", "-Fn", "-p", pids.map(String.init).joined(separator: ",")]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return [] }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        var urls: [URL] = []
        var seen = Set<String>()
        for line in text.split(separator: "\n") where line.hasPrefix("n") {
            let path = String(line.dropFirst())
            guard path.contains("/.claude/projects/"), path.hasSuffix(".jsonl") else { continue }
            if seen.insert(path).inserted { urls.append(URL(fileURLWithPath: path)) }
        }
        return urls
    }

    // Codex has no per-session process to watch, so use its rollout freshness:
    // present if written within the present window, busy if within the codex busy
    // one. Rollouts are foldered by the session's START date, so a session that
    // began days ago but is still being written today lives in an OLDER day's
    // folder — scan the last several days, not just today/yesterday, or a
    // long-running session goes invisible.
    private static func codexSessions(now: Date) -> [LocalSession] {
        let base = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex/sessions")
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy/MM/dd"
        let hex = providerHex("openai-codex")
        var out: [LocalSession] = []
        var seen = Set<String>()
        for day in 0...6 {   // today + the previous 6 days (start-date foldering)
            let dir = base.appendingPathComponent(formatter.string(from: now.addingTimeInterval(Double(-day) * 86_400)))
            guard let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey]) else { continue }
            for file in files where file.lastPathComponent.hasPrefix("rollout-") && file.pathExtension == "jsonl" {
                guard let modified = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate else { continue }
                let age = now.timeIntervalSince(modified)
                guard age < localPresentWindow else { continue }
                let key = "local:codex:\(file.lastPathComponent)"
                guard seen.insert(key).inserted else { continue }
                out.append(LocalSession(key: key, hex: hex, busy: age < codexBusyWindow, order: 1))
            }
        }
        return out
    }

    private static func flagValue(_ flag: String, in tokens: [String]) -> String? {
        // "--flag value"
        if let index = tokens.firstIndex(of: flag), index + 1 < tokens.count {
            return tokens[index + 1]
        }
        // "--flag=value" — how headless launches pass the id (Hermes Desktop runs
        // `claude … --resume=<id>` / `--session-id=<id>`), which the space form missed.
        let prefix = flag + "="
        if let token = tokens.first(where: { $0.hasPrefix(prefix) }) {
            let value = String(token.dropFirst(prefix.count))
            return value.isEmpty ? nil : value
        }
        return nil
    }

    // opencode has no long-lived per-session process to watch, so read its local
    // store (system SQLite — opencode.db) directly: a session is running while its
    // row's time_updated is fresh, and BUSY (mid-turn) while parts/messages are
    // still streaming in — opencode persists every stream/tool event, so during a
    // turn the newest write is seconds old (sub-4s observed), well inside
    // localBusyWindow; the dot clears promptly once the turn ends. time_updated is
    // epoch MILLISECONDS. Top-level sessions only (parent_id IS NULL) so subagent
    // runs don't multiply dots. Fail-open everywhere: an absent store, a busy
    // writer lock, or an unreadable WAL simply yields no dots for this tick.
    private static func opencodeSessions(now: Date) -> [LocalSession] {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/share/opencode/opencode.db").path
        guard FileManager.default.fileExists(atPath: path) else { return [] }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK, db != nil else {
            sqlite3_close(db)
            return []
        }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        let sql = "SELECT id, time_updated FROM session WHERE parent_id IS NULL " +
                  "ORDER BY time_updated DESC LIMIT 10"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        let hex = providerHex("opencode")
        var out: [LocalSession] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let cId = sqlite3_column_text(stmt, 0) else { continue }
            let id = String(cString: cId)
            let updated = sqlite3_column_double(stmt, 1) / 1000.0
            let age = now.timeIntervalSince1970 - updated
            // Rows come newest-first; anything older than the present window
            // (and everything after it) is a long-closed session.
            guard age >= 0, age < localPresentWindow else { break }
            out.append(LocalSession(
                key: "local:opencode:\(id)", hex: hex,
                busy: age < opencodeBusyWindow, order: 2))
        }
        return out
    }

    // A "not working" pet: the error/struggling animation + red halo + "!" badge
    // (drawPet/drawNukey render status "failed" that way), signalling the source
    // is disconnected and needs a sign-in.
    private static func needsLoginPlaceholder(gateway: String) -> ProviderActivityInstance {
        ProviderActivityInstance(
            completedAt: nil,
            key: "needs-login",
            model: "",
            profile: gateway,
            provider: "provider",
            providerColor: "#cf2d56",
            providerLabel: "",
            sessionId: "",
            startedAt: Date().timeIntervalSince1970,
            status: "failed",
            title: "Sign in"
        )
    }

    // A connected-but-idle source pet: calm idle animation, no badge.
    private static func idleSourceInstance(name: String) -> ProviderActivityInstance {
        ProviderActivityInstance(
            completedAt: nil,
            key: "\(name):idle",
            model: "",
            profile: name,
            provider: "source",
            providerColor: "#8e8e93",
            providerLabel: name,
            sessionId: "",
            startedAt: Date().timeIntervalSince1970,
            status: "sleeping",
            title: "Not in use"
        )
    }

    private func refreshDesktopStatus() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let desktop = Self.fetchDesktopStatus()
            DispatchQueue.main.async {
                guard let self else { return }
                self.desktopStatus = desktop
                self.updateStatusItem()
                self.rebuildMenu()
            }
        }
    }

    @objc private func toggleProviderExpanded(_ sender: NSButton) {
        guard let provider = sender.identifier?.rawValue else { return }
        if expandedProviders.contains(provider) {
            expandedProviders.remove(provider)
        } else {
            expandedProviders.insert(provider)
        }
        rebuildMenu()
    }

    private func refresh() {
        guard !refreshing else { return }
        refreshing = true
        let kinds = enabledGateways()   // fetch EVERY enabled source
        // A source with no data yet is LOADING — mark it activating so its pet stays
        // hidden (and its section shows a loading row) until data lands, instead of
        // flashing the "error" pet during the first fetch. Sources that already have
        // data keep their pet through a periodic refresh.
        for kind in kinds where !sources.contains(where: { $0.kind == kind }) {
            activating.insert(kind)
        }
        updateStatusItem()
        rebuildMenu()
        updateActivityPets()   // reflect the hidden pet immediately
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let desktop = Self.fetchDesktopStatus()
            let results: [(kind: GatewayKind, result: Result<QuotaPayload, Error>)] =
                kinds.map { ($0, Self.fetchPayload(kind: $0)) }
            DispatchQueue.main.async {
                guard let self else { return }
                self.refreshing = false
                self.desktopStatus = desktop
                var built: [SourceQuota] = []
                for (kind, result) in results {
                    switch result {
                    case .success(let payload):
                        let providers = self.applyLastGood(payload.providers, kind: kind)
                        built.append(SourceQuota(kind: kind, providers: providers, connected: true, generatedAt: payload.generatedAt))
                        // Remember this source's provider list for a later disconnect.
                        self.lastProvidersByKind[kind.rawValue] = providers.map { ($0.provider, $0.label) }
                        UserDefaults.standard.set(payload.providers.map { $0.provider }, forKey: "lastProviderSlugs.\(kind.rawValue)")
                        UserDefaults.standard.set(payload.providers.map { $0.label }, forKey: "lastProviderLabels.\(kind.rawValue)")
                    case .failure:
                        // Source down → list its known providers as Disconnected.
                        built.append(SourceQuota(kind: kind, providers: self.disconnectedProviders(for: kind), connected: false, generatedAt: nil))
                    }
                }
                // Keep only sources still enabled at completion — a source disabled
                // while this fetch was in flight must not reappear (the toggle-off lag).
                self.sources = built.filter { self.gatewayEnabled($0.kind) }
                self.noteQuotaTransitions()   // catch any provider that just reset
                self.activating.removeAll()   // data landed → clear the spinners
                self.updateStatusItem()
                self.rebuildMenu()
                self.updateActivityPets()
            }
        }
    }

    // Fetch from the user's active source only — no cross-fallback. Hermes goes
    // through the Desktop gateway session; Local reads the providers directly from
    // this Mac's credentials. If that source is unavailable we return a failure so
    // the app shows the pet + a sign-in prompt rather than
    // substituting the other gateway's providers.
    private static func fetchPayload(kind: GatewayKind) -> Result<QuotaPayload, Error> {
        let helper = kind == .local ? "hermes-local-quotas" : "hermes-desktop-quotas"
        let result = runHelper(helper)
        if case .success(let payload) = result, payload.providers.isEmpty {
            let msg = kind == .local ? "Sign in to your local providers to see quotas" : "Sign in to Hermes to see quotas"
            return .failure(NSError(domain: "ProviderQuotaMenuBar", code: 1, userInfo: [NSLocalizedDescriptionKey: msg]))
        }
        return result
    }

    // Locate a helper across the places the installers put it: ~/.local/bin
    // (install-menubar.sh) and Homebrew's bin (brew install). First existing +
    // executable wins; falls back to ~/.local/bin.
    static func helperPath(_ name: String) -> String {
        let fm = FileManager.default
        let fallback = fm.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/\(name)").path
        var candidates = [fallback, "/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)"]
        if let path = ProcessInfo.processInfo.environment["PATH"] {
            candidates += path.split(separator: ":").map { "\($0)/\(name)" }
        }
        return candidates.first { fm.isExecutableFile(atPath: $0) } ?? fallback
    }

    private static func runHelper(_ name: String) -> Result<QuotaPayload, Error> {
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        let helper = helperPath(name)
        guard FileManager.default.isExecutableFile(atPath: helper) else {
            return .failure(NSError(domain: "ProviderQuotaMenuBar", code: 127, userInfo: [NSLocalizedDescriptionKey: "\(name) is not installed"]))
        }
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["-lc", "exec \"$0\"", helper]
        process.standardOutput = output
        process.standardError = errors
        do {
            try process.run()
            process.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            if process.terminationStatus != 0 {
                let errorData = errors.fileHandleForReading.readDataToEndOfFile()
                let message = String(data: errorData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "\(name) request failed"
                throw NSError(domain: "ProviderQuotaMenuBar", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: message])
            }
            return .success(try JSONDecoder().decode(QuotaPayload.self, from: data))
        } catch {
            return .failure(error)
        }
    }

    private static func fetchDesktopStatus() -> DesktopStatus {
        let running = NSWorkspace.shared.runningApplications.contains {
            $0.bundleIdentifier == "com.nousresearch.hermes" || $0.executableURL?.lastPathComponent == "Hermes"
        }
        let support = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/Hermes")
        let storageURL = support.appendingPathComponent("Local Storage/leveldb")
        let values = readDesktopStorage(at: storageURL)
        let profile = values["hermes-desktop-active-profile-v1"]
        let provider = values["hermes.desktop.composer.provider"]
        let model = values["hermes.desktop.composer.model"]

        // Adopt whichever gateway THIS Desktop is bound to, from its own config —
        // prefer the v2 connections.json ({primary, connections:[{kind,url}]}),
        // fall back to the legacy connection.json — exactly like the quota helper,
        // so ANY Hermes Desktop setup works, not one specific machine/gateway.
        if let data = try? Data(contentsOf: support.appendingPathComponent("connections.json")),
           let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let list = root["connections"] as? [[String: Any]], !list.isEmpty {
            let primaryID = root["primary"] as? String
            let primary = list.first { ($0["id"] as? String) == primaryID } ?? list[0]
            if (primary["kind"] as? String) == "local" {
                return DesktopStatus(running: running, mode: "local", authMode: "local", remoteURL: nil, signedIn: true, reachable: running, profile: profile, provider: provider, model: model)
            }
            let remoteURL = primary["url"] as? String
            let authMode = (primary["authMode"] as? String) ?? "oauth"
            let signedIn = remoteURL.map { hasNativeOAuthSession(for: $0, support: support) } ?? false
            let reachable = remoteURL.map { gatewayIsReachable($0) } ?? false
            return DesktopStatus(running: running, mode: "remote", authMode: authMode, remoteURL: remoteURL, signedIn: signedIn, reachable: reachable, profile: profile, provider: provider, model: model)
        }

        // Legacy connection.json fallback.
        let connectionURL = support.appendingPathComponent("connection.json")
        guard let data = try? Data(contentsOf: connectionURL),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return DesktopStatus(running: running, mode: "unknown", authMode: "unknown", remoteURL: nil, signedIn: false, reachable: false, profile: profile, provider: provider, model: model)
        }
        let globalMode = root["mode"] as? String ?? "local"
        let profiles = root["profiles"] as? [String: Any]
        let scoped = profile.flatMap { profiles?[$0] as? [String: Any] }
        let block = scoped ?? (root["remote"] as? [String: Any] ?? [:])
        let mode = scoped?["mode"] as? String ?? globalMode
        if mode == "local" {
            return DesktopStatus(running: running, mode: mode, authMode: "local", remoteURL: nil, signedIn: true, reachable: running, profile: profile, provider: provider, model: model)
        }
        let remoteURL = block["url"] as? String
        let authMode = block["authMode"] as? String ?? "token"
        let signedIn: Bool
        if mode == "ssh" {
            signedIn = block["host"] as? String != nil
        } else if authMode == "oauth" {
            signedIn = remoteURL.map { hasNativeOAuthSession(for: $0, support: support) } ?? false
        } else {
            let token = block["token"] as? [String: Any]
            signedIn = !(token?["value"] as? String ?? "").isEmpty
        }
        let reachable = remoteURL.map { gatewayIsReachable($0) } ?? (mode == "ssh" && signedIn)
        return DesktopStatus(running: running, mode: mode, authMode: authMode, remoteURL: remoteURL, signedIn: signedIn, reachable: reachable, profile: profile, provider: provider, model: model)
    }

    private static func readDesktopStorage(at directory: URL) -> [String: String] {
        let keys = ["hermes-desktop-active-profile-v1", "hermes.desktop.composer.provider", "hermes.desktop.composer.model"]
        guard let files = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles]) else { return [:] }
        let candidates = files.filter { ["log", "ldb"].contains($0.pathExtension) }.sorted {
            let left = (try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            let right = (try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return left > right
        }
        var result: [String: String] = [:]
        for file in candidates {
            let process = Process()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/strings")
            process.arguments = ["-a", file.path]
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            guard (try? process.run()) != nil else { continue }
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let text = String(data: data, encoding: .utf8) ?? ""
            let lines = text.components(separatedBy: .newlines)
            for index in lines.indices {
                for key in keys where result[key] == nil && lines[index].hasPrefix(key) {
                    for valueIndex in lines.index(after: index)..<min(lines.count, index + 4) {
                        let value = lines[valueIndex].trimmingCharacters(in: .whitespacesAndNewlines)
                        if !value.isEmpty {
                            result[key] = value
                            break
                        }
                    }
                }
            }
            if result.count == keys.count { break }
        }
        return result
    }

    private static func hasNativeOAuthSession(for remoteURL: String, support: URL) -> Bool {
        let tokenURL = support.appendingPathComponent("native-oauth-tokens.json")
        guard let data = try? Data(contentsOf: tokenURL),
              let tokens = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        let normalized = remoteURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return tokens.keys.contains { $0.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == normalized }
    }

    private static func gatewayIsReachable(_ remoteURL: String) -> Bool {
        guard var components = URLComponents(string: remoteURL) else { return false }
        let path = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = path.isEmpty ? "/api/status" : "/\(path)/api/status"
        guard let url = components.url else { return false }
        let semaphore = DispatchSemaphore(value: 0)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 6
        configuration.timeoutIntervalForResource = 6
        var reachable = false
        let task = URLSession(configuration: configuration).dataTask(with: url) { _, response, _ in
            if let status = (response as? HTTPURLResponse)?.statusCode {
                reachable = (200..<500).contains(status)
            }
            semaphore.signal()
        }
        task.resume()
        if semaphore.wait(timeout: .now() + 7) == .timedOut {
            task.cancel()
        }
        return reachable
    }

    @objc private func openHermes() {
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: NSHomeDirectory() + "/Applications/Hermes.app"), configuration: NSWorkspace.OpenConfiguration())
    }

    // The Hermes app to launch / sign into ("local" has no app).
    private func gatewayAppURL() -> URL? {
        let hermes = URL(fileURLWithPath: NSHomeDirectory() + "/Applications/Hermes.app")
        return FileManager.default.fileExists(atPath: hermes.path) ? hermes
            : NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.nousresearch.hermes")
    }

    // Sources are configured by the user, per source — nothing is hardcoded or
    // assumed present. Both default OFF; you enable Hermes and/or Local in the
    // menu and the app uses ALL the enabled ones.
    private func gatewayEnabled(_ kind: GatewayKind) -> Bool {
        UserDefaults.standard.bool(forKey: "gatewayEnabled.\(kind.rawValue)")
    }

    private func setGatewayEnabled(_ kind: GatewayKind, _ on: Bool) {
        UserDefaults.standard.set(on, forKey: "gatewayEnabled.\(kind.rawValue)")
    }

    private func enabledGateways() -> [GatewayKind] {
        [.hermes, .local].filter { gatewayEnabled($0) }
    }

    // Whether a provider's dot appears in the CLOSED menu-bar status item
    // (defaults on). Toggled by the eye icon on each provider row.
    private func providerShownInMenuBar(_ kind: GatewayKind, _ slug: String) -> Bool {
        let key = "menuBarHidden.\(providerKey(kind, slug))"
        return UserDefaults.standard.object(forKey: key) == nil
            || !UserDefaults.standard.bool(forKey: key)
    }

    private func setProviderShownInMenuBar(_ kind: GatewayKind, _ slug: String, _ shown: Bool) {
        UserDefaults.standard.set(!shown, forKey: "menuBarHidden.\(providerKey(kind, slug))")
    }

    @objc private func toggleProviderMenuBar(_ sender: NSButton) {
        guard let raw = sender.identifier?.rawValue else { return }
        let parts = raw.split(separator: ":", maxSplits: 1).map(String.init)
        guard parts.count == 2, let kind = GatewayKind(rawValue: parts[0]) else { return }
        setProviderShownInMenuBar(kind, parts[1], !providerShownInMenuBar(kind, parts[1]))
        updateStatusItem()
        rebuildMenu()
        // Reflect the change in the pet IMMEDIATELY — rebuild from the cached
        // sessions (Hermes hexes + last local scan) with the new hidden filter, so
        // a hidden provider's dots vanish on click, not on the next 1s poll.
        activityPanel.show(buildSourcePetTiles(localSessions: lastLocalSessions))
    }

    @objc private func openGateway() {
        guard let url = gatewayAppURL() else { return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }

    // Route to the Hermes Desktop gateway page to set up / repair the gateway.
    // Prefer the Desktop app's gateway settings deep link (Hermes is the handler
    // for hermes://), falling back to opening the app.
    @objc private func openGatewaySetup() {
        if let url = URL(string: "hermes://settings/gateway"),
           NSWorkspace.shared.urlForApplication(toOpen: url) != nil {
            NSWorkspace.shared.open(url)
        } else {
            openHermes()
        }
    }

    private func openHermesSession(_ instance: ProviderActivityInstance) {
        guard !instance.sessionId.isEmpty else {
            openHermes()
            return
        }
        var components = URLComponents()
        components.scheme = "hermes"
        components.host = "session"
        components.path = "/\(instance.sessionId)"
        components.queryItems = [URLQueryItem(name: "profile", value: instance.profile)]
        guard let url = components.url else { return }
        NSWorkspace.shared.open(url)
    }

    private func gatewayAuthURL(action: String) -> URL? {
        guard let remoteURL = desktopStatus.remoteURL else { return nil }
        var components = URLComponents()
        components.scheme = "hermes"
        components.host = "gateway-auth"
        components.path = "/\(action)"
        components.queryItems = [URLQueryItem(name: "url", value: remoteURL)]
        return components.url
    }

    private func scheduleAuthRefresh() {
        authRefreshTimers.forEach { $0.invalidate() }
        authRefreshTimers = [2, 6, 15, 30, 60].map { delay in
            Timer.scheduledTimer(withTimeInterval: TimeInterval(delay), repeats: false) { [weak self] _ in
                self?.refresh()
            }
        }
    }

    // Sign in / out of the Hermes gateway via its OAuth deep-links. A local
    // Hermes backend with no remote URL just opens the app to sign in.
    @objc private func signInGateway() {
        guard let url = gatewayAuthURL(action: "login") else {
            openGateway()
            return
        }
        NSWorkspace.shared.open(url)
        scheduleAuthRefresh()
    }

    @objc private func signOutGateway() {
        guard let url = gatewayAuthURL(action: "logout") else {
            openGateway()
            return
        }
        NSWorkspace.shared.open(url)
        sources.removeAll { $0.kind == .hermes }
        updateStatusItem()
        rebuildMenu()
        scheduleAuthRefresh()
    }

    @objc private func quit() {
        NSApplication.shared.terminate(nil)
    }
}

@main
struct ProviderQuotaMenuBarApp {
    static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.accessory)
        application.run()
    }
}
