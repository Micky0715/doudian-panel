# API-first cloud material pipeline (MVP)

## Boundary

1. **Feishu Drive is the source of truth**.  This connector is read-only: list
   and download only. It cannot rename, move, delete or synchronise cloud files.
2. **A store main image is preferred.** If it is absent, the controlled
   browser fallback applies: the first of four valid, distinct white-background
   images becomes the cover and is reported as a substitution. An existing but
   invalid `主图.*` blocks instead of silently falling back.
3. The image must travel as **Feishu bytes → DouDian material centre → DouDian
   `byte_url` → `product.editV2(pic=...)`**. A Feishu link/local path is never
   written to a product.
4. The first product-image writer only saves a draft (`commit=false`). Submit
   remains the existing immutable publication boundary after API read-back.
5. Browser upload remains available only as a separately selected, human
   confirmed contingency; it is not an automatic fallback from this module.

## MVP source layout

```text
<root>/
├── 白底图库/手机/<brand>/<model>/白底图/白底图_01.jpg ... 白底图_04.jpg
└── 抖音主图/<shop_id>/<brand>/<model>/主图/主图.jpg
```

This is a strict default based on the live white library. Operators may name a
model folder with its brand prefix (for example `苹果 iPhone 15` or
`三星Galaxy S25`); the resolver compares the complete normalised model key,
not a prefix, and blocks if more than one folder has the same key. If the main
library is deliberately maintained as `<shop_id>/<model>`, set
`FEISHU_MAIN_PATH_LAYOUT=shop_model`; the resolver never guesses between the
two layouts. The canonical Apple path is `白底图库/手机/苹果/<model>`; any
deployment-specific exception must be declared in `FEISHU_BRAND_FOLDER_MAP_JSON`.

Runtime lookup starts from the configured root, anchors the main library at an
exact folder named `抖音主图`, and, when an expected folder is not present at
the current level, continues downward through wrapper folders. Search is
breadth-first and bounded; the nearest exact match wins, while two matches at
the same nearest depth block instead of being chosen by directory order. The
same rule applies to the shop, optional brand, model and named image folders,
so layouts may contain operational grouping levels without weakening model
identity checks.

`主图` and `白底图` are explicit child folders in the live library.  A single
supported image inside an explicit `主图` folder is accepted even if its
filename is the model name; two unnamed candidates block the listing.  During
migration only, a model folder that has no such child folder can retain the
flat `主图.*` / `白底图_*.*` layout.  The resolver never mixes the two folders
or searches another model to make up a missing image.

The material indexer verifies these exact paths and records each file token,
revision, digest, material ID and DouDian URL. No folder layout is inferred by
fuzzy filename matching.  Before every draft write it downloads the planned
file again and checks its digest, so an operator replacing a Feishu file midway
through a batch cannot silently change the submitted material. Main-area
pictures are checked before upload for format, the DouDian 3 MiB limit, a
square shape and minimum 600×600 pixels. An invalid extra white image is
omitted with its reason when the remaining set still satisfies the 1+3 (or
white-only 4) minimum. Cache evidence is scoped to both shop and material
folder, and a cached material ID is re-read from DouDian before it is reused.

## Model-folder resolution

Unattended jobs use one bounded hierarchy: (1) the complete parsed model must
match one folder; (2) after an exact miss only, one trailing edition label such
as `5G版` or `活力版` may be removed, and the resulting folder must be unique;
(3) a reviewed manufacturer-site name from `official_models.json` may break a
literal folder-name tie. If none of these produces one result, the product is
sent to the manual queue. Core variants such as Pro, Max, Ultra, Fold, Flip and
`+` are never removed. A suffix-removed cloud folder is additionally rejected
unless all platform colour values match its image filenames, preventing a base
model's pictures from being written to a genuinely different edition.
