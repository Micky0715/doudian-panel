import unittest
from pathlib import Path

from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]


class PanelMascotUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "panel.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "assets" / "panel.css").read_text(encoding="utf-8")
        cls.script = (ROOT / "assets" / "panel.js").read_text(encoding="utf-8")
        cls.mascot_script = (ROOT / "assets" / "mascot.js").read_text(encoding="utf-8")

    def test_transparent_character_assets_and_layers_exist(self):
        character_dir = ROOT / "assets" / "character"
        webm_names = (
            "character-full-action-transparent.webm",
            "character-box-open-transparent.webm",
        )
        for filename in webm_names:
            asset = character_dir / filename
            self.assertTrue(asset.is_file(), filename)
            self.assertTrue(asset.read_bytes().startswith(b"\x1aE\xdf\xa3"), filename)
            self.assertIn("/assets/character/" + filename, self.html)

        for filename in (
            "character-idle-eye-base.webp",
            "character-idle-forward.webp",
            "left-iris.webp",
            "right-iris.webp",
        ):
            asset = character_dir / filename
            self.assertTrue(asset.is_file(), filename)
            self.assertEqual(asset.read_bytes()[:4], b"RIFF")
            self.assertIn("/assets/character/" + filename, self.html)
        with Image.open(character_dir / "left-iris.webp") as left_iris, Image.open(
            character_dir / "right-iris.webp"
        ) as right_iris:
            self.assertEqual(left_iris.size, (32, 32))
            self.assertEqual(right_iris.size, left_iris.size)
            for iris in (left_iris.convert("RGBA"), right_iris.convert("RGBA")):
                opaque = [pixel for pixel in iris.getdata() if pixel[3] >= 128]
                self.assertTrue(any(max(pixel[:3]) < 55 for pixel in opaque))
                self.assertTrue(any(pixel[0] > pixel[1] + 8 for pixel in opaque))
                self.assertTrue(any(min(pixel[:3]) > 215 for pixel in opaque))
        with Image.open(character_dir / "character-idle-forward.webp") as forward, Image.open(
            character_dir / "character-idle-eye-base.webp"
        ) as eye_base:
            self.assertEqual(forward.size, (642, 702))
            self.assertEqual(forward.mode, "RGBA")
            self.assertIsNone(
                ImageChops.difference(
                    forward.convert("RGBA").getchannel("A"),
                    eye_base.convert("RGBA").getchannel("A"),
                ).getbbox()
            )
            clean_base = eye_base.convert("RGB")
            right_eye_residual_zone = clean_base.crop((399, 197, 417, 211))
            self.assertGreater(
                min(
                    pixel[0] * .2126 + pixel[1] * .7152 + pixel[2] * .0722
                    for pixel in right_eye_residual_zone.getdata()
                ),
                175,
            )
        self.assertTrue((character_dir / "character-wave-transparent.webm").is_file())
        self.assertTrue((character_dir / "character-animation-forward-frame.png").is_file())
        self.assertEqual(self.html.count('class="character-layer '), 4)
        self.assertEqual(self.html.count('class="character-eye-mask '), 2)
        self.assertIn('data-character-state="idle"', self.html)
        self.assertIn(
            '<link rel="preload" as="image" href="/assets/character/character-idle-forward.webp?v=20260911-eye4" type="image/webp">',
            self.html,
        )
        self.assertIn('fetchpriority="high" decoding="sync"', self.html)
        self.assertLess(
            self.html.index("character-idle-forward.webp"),
            self.html.index("character-idle-eye-base.webp"),
        )
        mascot_markup = self.html.split('id="mascotArt"', 1)[1].split("</figure>", 1)[0]
        self.assertEqual(mascot_markup.count("<video"), 2)
        self.assertEqual(mascot_markup.count(' muted playsinline preload="auto"'), 2)
        self.assertNotIn(" loop", mascot_markup)
        self.assertNotIn(" controls", mascot_markup)

        generator = (ROOT / "scripts" / "generate_character_eye_assets.py").read_text(encoding="utf-8")
        self.assertIn("character-animation-forward-frame.png", generator)
        self.assertIn("character-box-open-transparent.webm", generator)
        self.assertIn("aihuishou-ops-mascot-v4-wanwu.png", generator)
        self.assertIn("_apply_reference_eye_patch", generator)
        self.assertNotIn("def _restore_eye_shape", generator)
        self.assertIn('id="characterLeftEyeClip"', self.html)
        self.assertIn('id="characterRightEyeClip"', self.html)
        self.assertIn('data-down-inset="1.5"', self.html)
        self.assertIn('clipPathUnits="objectBoundingBox"', self.html)

    def test_homepage_has_centered_text_only_shortcuts(self):
        self.assertIn('class="ops-orbit"', self.html)
        self.assertIn('class="ops-rail ops-rail-left"', self.html)
        self.assertIn('class="ops-rail ops-rail-right"', self.html)
        shortcuts = self.html.count('<button class="ops-launch"') + self.html.count('<a class="ops-launch"')
        self.assertEqual(shortcuts, 2)
        self.assertNotIn("ops-launch-icon", self.html)
        self.assertNotIn("ops-launch-no", self.html)
        self.assertIn(
            "grid-template-columns:minmax(150px,1fr) minmax(0,700px) minmax(150px,1fr)",
            self.css,
        )
        self.assertIn("column-gap:clamp(20px,2.5vw,44px)", self.css)
        self.assertIn("height:clamp(550px,73vh,770px)", self.css)
        self.assertIn("width:min(100%,700px)", self.css)
        for label in ("盖亚铺货", "抖店上架"):
            self.assertIn(label, self.html)
        for removed in ("素材图库", "标准型号库", "API 型号体检", "访问地址与开机自启", "异常人工队列"):
            self.assertNotIn(removed, self.html)

    def test_no_legacy_sprite_or_overlay_implementation_remains(self):
        combined = self.html + self.css + self.script + self.mascot_script
        for forbidden in (
            "mascot-head",
            "mascot-pupil",
            "mascot-product",
            "mascot-box-lid",
            "mascot-wave-arm",
            "data-interaction-state",
            "mix-blend-mode",
            "/assets/preview-products/",
        ):
            self.assertNotIn(forbidden, combined)

    def test_navigation_and_status_are_plain(self):
        topbar = self.html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
        self.assertIn('<div class="logo" aria-hidden="true">', topbar)
        self.assertIn('<svg viewBox="0 0 64 64"', topbar)
        self.assertNotIn('<div class="logo">爱</div>', topbar)
        self.assertIn('id="dot"', topbar)
        self.assertIn('id="connTxt"', topbar)
        self.assertIn('id="allErrBtn"', topbar)
        self.assertIn('<span class="ae-count">0</span>', topbar)
        self.assertIn('class="ops-statusline"', self.html)
        self.assertIn("border-bottom:1px solid rgba(24,52,33,.09)", self.css)

    def test_single_character_state_machine_serializes_video_playback(self):
        for state in ("IDLE", "EYE_TRACKING", "BOX_OPENING", "WAVING", "RETURNING"):
            self.assertIn(state, self.mascot_script)
        for forbidden in ("GREETING_FULL", "GREETING_OPENED", "OPENED", "FALLBACK"):
            self.assertNotIn(forbidden, self.mascot_script)
        self.assertIn("let characterState = STATES.IDLE", self.mascot_script)
        self.assertIn("let resourcesReady = false", self.mascot_script)
        self.assertIn("initializeResources()", self.mascot_script)
        self.assertIn("waitForImage(forwardLayer)", self.mascot_script)
        self.assertIn("...eyes.map(eye => waitForImage(eye.iris))", self.mascot_script)
        self.assertIn("enableGlobalGaze()", self.mascot_script)
        self.assertIn("art.dataset.characterState = nextState", self.mascot_script)
        self.assertIn("}, 120);", self.mascot_script)
        self.assertIn("event.stopPropagation()", self.mascot_script)
        self.assertIn("await video.play()", self.mascot_script)
        self.assertIn("videos.forEach(pauseAndReset)", self.mascot_script)
        self.assertIn("new AbortController()", self.mascot_script)
        self.assertIn("new ResizeObserver", self.mascot_script)
        self.assertIn("let hasPointerMoved = false", self.mascot_script)
        self.assertIn("(bounds.width - irisWidth) / 2 - safetyInset", self.mascot_script)
        self.assertIn("(bounds.height - irisHeight) / 2 - safetyInset", self.mascot_script)
        self.assertIn("safeRangeX * .85", self.mascot_script)
        self.assertIn("safeRangeY * .75", self.mascot_script)
        self.assertIn("Math.min(window.innerWidth, window.innerHeight) * .32", self.mascot_script)
        self.assertIn("(x / eye.maxX) ** 2 + (y / verticalRange) ** 2", self.mascot_script)
        self.assertIn("const easing = .16", self.mascot_script)
        self.assertIn("window.addEventListener('pointermove', handleGlobalPointerMove", self.mascot_script)
        self.assertIn("document.documentElement.addEventListener('pointerleave'", self.mascot_script)
        self.assertIn("document.addEventListener('visibilitychange'", self.mascot_script)
        self.assertNotIn("art.addEventListener('pointermove'", self.mascot_script)
        self.assertIn("translate3d(var(--gaze-x,0),var(--gaze-y,0),0)", self.css)
        self.assertIn("aspect-ratio:1/1;object-fit:contain;border-radius:50%", self.css)
        self.assertIn("--iris-size:clamp(25.07px,3.327vh,35.10px)", self.css)
        self.assertIn("--iris-scale:1.21875", self.css)
        self.assertIn("--iris-scale:1.28125", self.css)
        self.assertIn("irisBounds.width * (eye.sourceIrisWidth / 32)", self.mascot_script)
        self.assertIn('clip-path:url("#characterLeftEyeClip")', self.css)
        self.assertIn('clip-path:url("#characterRightEyeClip")', self.css)
        self.assertIn("eye.maxDownY = Math.max(0, eye.maxY - eye.sourceDownInset * scaleY)", self.mascot_script)
        self.assertIn("window.addEventListener('pagehide', cleanup", self.mascot_script)
        self.assertIn('<script src="/assets/mascot.js"></script>', self.html)
        self.assertNotIn("  initMascotExperience();", self.script)

    def test_breakpoints_and_reduced_motion_are_present(self):
        self.assertIn("@media(max-width:1023px)", self.css)
        self.assertIn("@media(max-width:767px)", self.css)
        self.assertIn("@media(max-height:759px) and (min-width:1024px)", self.css)
        self.assertIn('grid-template-areas:"mascot mascot" "left right"', self.css)
        self.assertIn('grid-template-areas:"mascot" "left" "right"', self.css)
        self.assertIn("prefers-reduced-motion:reduce", self.css)
        self.assertIn('data-character-state="boxOpening"', self.css)
        self.assertIn('data-character-state="waving"', self.css)


if __name__ == "__main__":
    unittest.main()
