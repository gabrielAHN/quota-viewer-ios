class ProviderQuotas < Formula
  desc "macOS menu-bar app for live provider quotas + Hermes session pets"
  homepage "https://github.com/gabrielAHN/quota-viewer-ios"
  license "MIT"

  # No tagged release yet — install with --HEAD (builds from main):
  #   brew install --HEAD gabrielahn/quota/provider-quotas
  # When a version is tagged, add a stable source + checksum here:
  #   url "https://github.com/gabrielAHN/quota-viewer-ios/archive/refs/tags/v0.2.0.tar.gz"
  #   sha256 "<shasum -a 256 of that tarball>"
  head "https://github.com/gabrielAHN/quota-viewer-ios.git", branch: "main"

  depends_on :macos

  def install
    # Build the Swift menu-bar binary (uses the Xcode command-line tools that
    # Homebrew already requires).
    system "xcrun", "swiftc", "-O", "-parse-as-library", "-framework", "AppKit",
           "menubar/ProviderQuotaMenuBar.swift", "-o", "ProviderQuotaMenuBar"

    # Assemble the .app bundle so the app runs under its OWN bundle identifier
    # (its UserDefaults domain) and Info.plist (LSUIElement menu-bar agent).
    app = prefix/"Provider Quotas.app"
    (app/"Contents/MacOS").install "ProviderQuotaMenuBar"
    (app/"Contents").install "menubar/Info.plist"
    system "codesign", "--force", "--sign", "-", app

    # Data helpers the app shells out to (the app finds them here, on PATH).
    bin.install "desktop-quotas.sh" => "hermes-desktop-quotas"
    bin.install "local-quotas.sh" => "hermes-local-quotas"
    bin.install "provider-quotas.sh" => "provider-quotas"

    # Shared pet bootstrap (run in post_install).
    libexec.install "bootstrap-pets.sh"
  end

  def post_install
    # Curated pets so the picker has options — best-effort, never fatal.
    system "bash", libexec/"bootstrap-pets.sh"
  end

  service do
    run [opt_prefix/"Provider Quotas.app/Contents/MacOS/ProviderQuotaMenuBar"]
    keep_alive true
    log_path var/"log/provider-quotas.log"
    error_log_path var/"log/provider-quotas-error.log"
  end

  def caveats
    <<~EOS
      Start it in the menu bar (and run it at login):
        brew services start provider-quotas

      Click the menu-bar icon, then turn on Hermes and/or Local under "Sources".
      Debug from the terminal:
        provider-quotas              (quotas as the menu reads them)
        provider-quotas doctor       (install, service, sources, gateway, logs)
      Update later with:
        brew upgrade provider-quotas        (or the in-app "Check for Updates")

      On a Hermes GATEWAY host, also install the dashboard plugin:
        git clone https://github.com/gabrielAHN/quota-viewer-ios
        cd quota-viewer-ios && ./install.sh
    EOS
  end

  test do
    assert_path_exists bin/"hermes-desktop-quotas"
    assert_path_exists bin/"hermes-local-quotas"
    assert_match "usage: provider-quotas", shell_output("#{bin}/provider-quotas --help")
    assert_predicate prefix/"Provider Quotas.app/Contents/MacOS/ProviderQuotaMenuBar", :executable?
  end
end
