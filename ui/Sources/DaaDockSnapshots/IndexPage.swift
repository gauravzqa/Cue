import Foundation

/// One page that lays out every snapshot, light and dark side by side.
enum IndexPage {
    static func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }

    static func write(_ entries: [Entry], to url: URL) throws {
        var groups: [String] = []
        for e in entries where !groups.contains(e.group) { groups.append(e.group) }

        var body = ""
        body += "<nav>" + groups.enumerated().map { "<a href=\"#g\($0.offset)\">\(esc($0.element))</a>" }
            .joined(separator: "") + "</nav>\n"
        for (gi, g) in groups.enumerated() {
            body += "<section id=\"g\(gi)\"><h2>\(esc(g))</h2>\n"
            for e in entries where e.group == g {
                body += "<article id=\"\(esc(e.name))\"><header><h3><a href=\"#\(esc(e.name))\">\(esc(e.name))</a></h3>"
                body += "<p>\(esc(e.note))</p></header><div class=\"pair\">"
                for scheme in ["light", "dark"] {
                    if let f = e.files[scheme] {
                        body += "<figure class=\"\(scheme)\"><a href=\"\(esc(f))\"><img src=\"\(esc(f))\" alt=\"\(esc(e.name)) \(scheme)\" loading=\"lazy\"></a>"
                        body += "<figcaption>\(scheme)</figcaption></figure>"
                    }
                }
                body += "</div></article>\n"
            }
            body += "</section>\n"
        }

        let html = """
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>daa dock snapshots</title>
        <style>
        :root { --bg: #f4f4f2; --fg: #1d1d1f; --muted: #6b6b70; --card: #ffffff; --line: #d9d9dc; }
        @media (prefers-color-scheme: dark) { :root { --bg: #161618; --fg: #ececee; --muted: #9a9aa0; --card: #1f1f22; --line: #333338; } }
        * { box-sizing: border-box; }
        body { margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--fg);
               font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; }
        h1 { font-size: 22px; margin: 0 0 4px; } .lede { color: var(--muted); margin: 0 0 16px; max-width: 70ch; }
        nav { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-bottom: 24px; }
        nav a { color: var(--muted); } h2 { font-size: 17px; margin: 36px 0 10px; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
        article { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 12px 0; }
        h3 { font: 600 13px ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; } h3 a { color: inherit; text-decoration: none; }
        article p { margin: 4px 0 10px; color: var(--muted); max-width: 90ch; }
        .pair { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }
        figure { margin: 0; padding: 14px; border-radius: 8px; max-width: 100%; }
        figure.light { background: #c9ccd2; } figure.dark { background: #3a3c42; }
        figure img { display: block; max-width: 100%; height: auto; }
        figcaption { font-size: 11px; color: #222; margin-top: 6px; } figure.dark figcaption { color: #ddd; }
        </style></head><body>
        <h1>daa dock snapshots</h1>
        <p class="lede">Rendered offscreen by <code>make snapshots</code> (Sources/DaaDockSnapshots): each view draws itself into a bitmap from a window that is never shown. No screenshot, no Screen Recording. Images are 2× (menu-bar glyphs 6×) — shown here at half their pixel size. Click an image for full size. Materials render flat offscreen; the grey backdrop is the page, not the app.</p>
        \(body)
        <script>document.querySelectorAll('figure img').forEach(i=>{i.addEventListener('load',()=>{const s=i.src.includes('menubar-')?6:2;i.style.width=(i.naturalWidth/s)+'px';});if(i.complete)i.dispatchEvent(new Event('load'));});</script>
        </body></html>
        """
        try html.write(to: url, atomically: true, encoding: .utf8)
    }
}
