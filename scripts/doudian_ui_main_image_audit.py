#!/usr/bin/env python3
"""Read-only browser verification for one draft's semantic main-image field.

This script deliberately has no click, set_input_files, save or publish path.
It exists as the final UI proof after an API image write: API readback confirms
the stored URLs, while this audit confirms the same draft editor renders a
preview in its ``商品正面图 / 上传主图`` semantic area.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class UIMainImageAuditError(RuntimeError):
    pass


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _snapshot_main_field(page) -> dict[str, Any]:
    """Read the unique semantic main area without interacting with the page."""
    return page.evaluate(
        """
        () => {
          const shopIds = new Set();
          try {
            for (let i = 0; i < localStorage.length; i++) {
              const key = localStorage.key(i) || '';
              if (!key.startsWith('__tea_cache_tokens_')) continue;
              try {
                const value = JSON.parse(localStorage.getItem(key) || '{}');
                const match = String(value.user_unique_id || '').match(/(?:^|_)(\\d{5,12})$/);
                if (match) shopIds.add(match[1]);
              } catch (_) {}
            }
          } catch (_) {}

          const inputs = [...document.querySelectorAll('input[type="file"]')];
          function inspect(input) {
            let node = input.closest('label') || input.parentElement;
            let semanticText = '', nearestVisible = null;
            for (let depth = 0; depth < 10 && node; depth++, node = node.parentElement) {
              const text = (node.innerText || '').trim();
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              if (!semanticText && text) semanticText = text.slice(0, 160);
              if (!nearestVisible && rect.width > 0 && rect.height > 0 &&
                  style.display !== 'none' && style.visibility !== 'hidden') {
                nearestVisible = node;
              }
            }
            let context = '';
            for (let depth = 0, parent = nearestVisible; depth < 4 && parent;
                 depth++, parent = parent.parentElement) {
              context += ' ' + (parent.innerText || '');
            }
            return {
              input, semanticText, context,
              active: Boolean(nearestVisible) && !input.disabled &&
                !input.closest('[aria-disabled="true"],[class*="disabled"]'),
              threeFour: /3\\s*[:：]\\s*4/.test(context),
            };
          }
          function previewInfo(scope) {
            let imageCount = 0, backgroundCount = 0;
            for (const image of scope.querySelectorAll('img')) {
              const rect = image.getBoundingClientRect();
              const source = image.currentSrc || image.getAttribute('src') || '';
              if (source && !source.startsWith('data:image/svg') && rect.width >= 30 && rect.height >= 30) imageCount++;
            }
            for (const element of scope.querySelectorAll('*')) {
              const rect = element.getBoundingClientRect();
              const background = getComputedStyle(element).backgroundImage || '';
              if (background.includes('url(') && rect.width >= 30 && rect.height >= 30) backgroundCount++;
            }
            return {imageCount, backgroundCount};
          }
          const semanticMains = inputs.map(inspect).filter(row =>
            row.active && !row.threeFour && row.semanticText.includes('商品正面图') &&
            row.semanticText.includes('上传主图')
          );
          const candidates = semanticMains.map(row => {
            const label = row.input.closest('label') || row.input.parentElement;
            const siblingContainer = label && label.parentElement;
            const siblingSample = siblingContainer ? [...siblingContainer.children].slice(0, 12).map(child => ({
              tag: child.tagName,
              class_name: String(child.className || '').slice(0, 160),
              text: (child.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
              input_count: child.querySelectorAll ? child.querySelectorAll('input[type="file"]').length : 0,
              img_count: child.querySelectorAll ? child.querySelectorAll('img').length : 0,
            })) : [];
            const ancestorSamples = [];
            let fallback = null;
            for (let depth = 0, parent = row.input.parentElement; depth < 8 && parent;
                 depth++, parent = parent.parentElement) {
              const text = (parent.innerText || '').trim();
              const inputCount = parent.querySelectorAll('input[type="file"]').length;
              const rect = parent.getBoundingClientRect();
              ancestorSamples.push({
                depth,
                tag: parent.tagName,
                class_name: String(parent.className || '').slice(0, 160),
                input_count: inputCount,
                img_count: parent.querySelectorAll('img').length,
                child_class_sample: [...parent.children].slice(0, 8).map(child =>
                  String(child.className || child.tagName || '').slice(0, 100)
                ),
                width: Math.round(rect.width), height: Math.round(rect.height),
                text: text.replace(/\\s+/g, ' ').slice(0, 120),
              });
              if (!fallback && inputCount === 1) fallback = parent;
              if (inputCount === 1 && (text.includes('商品正面图') || text.includes('上传主图'))) {
                fallback = parent;
                break;
              }
            }
            const preview = fallback ? previewInfo(fallback) : {imageCount: 0, backgroundCount: 0};
            return {
              text: row.semanticText,
              slot_image_count: preview.imageCount,
              slot_background_count: preview.backgroundCount,
              preview_present: preview.imageCount + preview.backgroundCount > 0,
              ancestor_samples: ancestorSamples,
              sibling_sample: siblingSample,
            };
          });
          const wrappers = [...document.querySelectorAll('.index-module_imgWrapper__xOFF7')];
          const firstWrapperPreview = wrappers.length ? previewInfo(wrappers[0]) : {imageCount: 0, backgroundCount: 0};
          const galleryCandidates = [];
          const galleryKeys = new Set();
          for (const label of [...document.querySelectorAll('*')]) {
            const labelText = (label.innerText || '').replace(/[\\s*]/g, '');
            if (labelText !== '主图') continue;
            for (let depth = 0, parent = label.parentElement; depth < 6 && parent;
                 depth++, parent = parent.parentElement) {
              const preview = previewInfo(parent);
              if (preview.imageCount + preview.backgroundCount > 0) {
                const candidate = {
                  image_count: preview.imageCount,
                  background_count: preview.backgroundCount,
                  preview_present: true,
                  text: (parent.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
                };
                const key = `${candidate.image_count}:${candidate.background_count}:${candidate.text}`;
                if (!galleryKeys.has(key)) {
                  galleryKeys.add(key);
                  galleryCandidates.push(candidate);
                }
                break;
              }
            }
          }
          return {
            url: location.href,
            title: document.title || '',
            body_excerpt: (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 700),
            file_input_count: inputs.length,
            shop_ids: [...shopIds],
            semantic_main_candidate_count: candidates.length,
            first_1_to_1_wrapper: {
              found: wrappers.length > 0,
              image_count: firstWrapperPreview.imageCount,
              background_count: firstWrapperPreview.backgroundCount,
              preview_present: firstWrapperPreview.imageCount + firstWrapperPreview.backgroundCount > 0,
            },
            main_gallery_candidates: galleryCandidates,
            candidates,
          };
        }
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DouDian draft main-image UI audit")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--profile", default="chrome_user_data")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output-root", default="panel_data/doudian_ui_main_image_audits")
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id).strip(), str(args.product_id).strip()
    if not shop_id.isdigit() or not product_id.isdigit():
        raise UIMainImageAuditError("shop_id and product_id must be numeric")

    from playwright.sync_api import sync_playwright

    report: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": int(time.time()),
        "shop_id": shop_id,
        "product_id": product_id,
        "read_only": True,
        "result": "blocked",
    }
    output = Path(args.output_root) / shop_id / f"{report['captured_at']}_{product_id}.json"
    screenshot = output.with_suffix(".png")
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(Path(args.profile).resolve()),
                headless=bool(args.headless),
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 1440, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(
                    f"https://fxg.jinritemai.com/ffa/g/create?product_id={product_id}&entrance=draft",
                    wait_until="domcontentloaded", timeout=30000,
                )
                try:
                    page.wait_for_function(
                        "document.querySelectorAll('input[type=file]').length > 0", timeout=20000,
                    )
                except Exception:
                    # The report includes a body excerpt in this case.  Do not
                    # convert an unfinished React render into a false success.
                    pass
                page.wait_for_timeout(2500)
                # The editor lazy-renders the image section below the long
                # property form.  Scrolling only materialises DOM; it never
                # focuses a field or changes any product value.
                snapshot = _snapshot_main_field(page)
                for _ in range(8):
                    if int(snapshot.get("semantic_main_candidate_count") or 0) > 0:
                        break
                    page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight * 0.85))")
                    page.wait_for_timeout(800)
                    snapshot = _snapshot_main_field(page)
                if int(snapshot.get("semantic_main_candidate_count") or 0) == 1:
                    page.evaluate(
                        """() => {
                          const input = [...document.querySelectorAll('input[type="file"]')].find(item => {
                            let node = item.closest('label') || item.parentElement;
                            let text = '';
                            for (let depth = 0; depth < 4 && node; depth++, node = node.parentElement) {
                              if (!text && (node.innerText || '').trim()) text = (node.innerText || '').trim();
                            }
                            return text.includes('商品正面图') && text.includes('上传主图');
                          });
                          const target = input && (input.closest('label') || input.parentElement);
                          if (target) target.scrollIntoView({block: 'center', inline: 'nearest'});
                        }"""
                    )
                    page.wait_for_timeout(700)
                    snapshot = _snapshot_main_field(page)
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=False)
            finally:
                context.close()
        report["snapshot"] = snapshot
        report["screenshot"] = str(screenshot)
        observed = {str(value) for value in snapshot.get("shop_ids") or []}
        if observed != {shop_id}:
            raise UIMainImageAuditError(
                f"profile shop identity mismatch: expected {shop_id}, observed {sorted(observed)}"
            )
        candidates = snapshot.get("candidates") or []
        if len(candidates) != 1:
            raise UIMainImageAuditError(
                f"semantic main-image field is not unique: {len(candidates)} candidates"
            )
        # A post-save page rebuild separates the preview wrapper from the file
        # input. Prefer a direct slot preview; otherwise accept the visible
        # gallery labelled “主图” only after the semantic input itself has been
        # uniquely resolved. This is a UI rendering check, not the causal write
        # proof used during the original upload operation.
        direct_preview = bool(candidates[0].get("preview_present"))
        wrapper_preview = bool((snapshot.get("first_1_to_1_wrapper") or {}).get("preview_present"))
        galleries = snapshot.get("main_gallery_candidates") or []
        gallery_preview = len(galleries) == 1 and bool(galleries[0].get("preview_present"))
        if not direct_preview and not wrapper_preview and not gallery_preview:
            raise UIMainImageAuditError("semantic main-image field has no visible preview")
        report["result"] = "verified_ui_main_preview"
    except Exception as exc:
        report["error"] = str(exc)
    _atomic_write(output, report)
    print(json.dumps({"result": report["result"], "report": str(output)}, ensure_ascii=False))
    return 0 if report["result"] == "verified_ui_main_preview" else 2


if __name__ == "__main__":
    raise SystemExit(main())
