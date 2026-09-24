# 抖店开放平台集成边界（阶段一）

本目录是盖亚新流程访问抖店开放平台的唯一入口。本阶段的目标是建立可靠的多店授权和只读商品查询地基，不替换现有浏览器上架流程。

## 当前允许

- `product.listV2`：读取商品/草稿候选列表；
- `product.detail`：读取商品详情，默认 `show_draft=true`；
- `sku.detail`：按 SKU ID 或编码读取 SKU；
- `product.getCatePropertyV2`：读取类目属性及枚举 ID，由独立属性目录层校验；
- 当前项目应用类型为 `self_use`；access token 进入到期前 1 小时窗口时，按店铺调用 `token.create + authorization_self` 更新；
- 无有效期元数据的旧 token 在平台返回过期后，同样只重建当前店铺，并将原读取请求重试一次。
- **本地/云素材迁移（草稿阶段）**：仅允许 `open.materialToken` → 临时 ImageX 字节上传 → `material.uploadImageSync` → `material.queryMaterialDetail`，将已验证的图片字节转为抖店 `byte_url`。素材文件夹 ID 必须显式配置，禁止按名称或位置猜测；
- **图片写入（草稿阶段）**：仅允许锁定单个商品的 `product.editV2(product_id, product_format_new, pic, commit=false)`，并在 API 回读时确认第一张图就是本次上传的店铺主图；失败后恢复原 `pic` 并转人工。

## 当前禁止

- 新增商品、下架商品、删除商品、SKU/价格/库存/运费/详情写入；
- 未经云素材验证直接填写图片 URL、飞书链接或本机路径；
- 云素材迁移后自动发布；发布仍必须经过单独的不可变计划和 API 回读；
- 工具型应用的 OAuth code 流程；
- API 查询成功直接等同于盖亚上传成功、草稿匹配成功或商品发布成功。

写方法不只是“约定不用”：只读方法仍被 `READ_ONLY_METHODS` 白名单限制；仅上述两个独立、锁定产品 ID 的写入边界可以保存草稿，二者都不能把 `commit` 变成 `true`。

## 飞书云图库接入

参见 [`../../app/material_pipeline/README.md`](../../app/material_pipeline/README.md)。生产环境只需要读权限，配置项如下（密钥只写入本机 `env.local`）：

```text
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_MATERIAL_ROOT_FOLDER_TOKEN=
```

目录结构和文件名必须精确匹配。缺主图、少于三张白底图、存在内容重复、文件版本改变或抖店素材 URL 无法回读，都会阻断本次草稿写入；浏览器不自动接手。

## 验收顺序

1. `python3 -m unittest tests.test_doudian_integration -v`
2. `python3 -m unittest discover -s tests -p 'test_*.py' -v`
3. 授权有效后：`python3 -m scripts.doudian_readonly_check <店铺ID> --detail`

第三步只读取一条商品和该商品详情。输出不会包含 app secret、access token 或 refresh token。

## 授权失败处理

`31008 token已过期` 表示旧 refresh token 已失效。当前应用已经确认全部为自用型，因此系统不再依赖旧 refresh token，而是按已绑定的店铺 ID 调用 `token.create + authorization_self`。平台返回的 `shop_id` 必须与目标店铺完全一致才会原子保存；不一致或请求失败时原 token 文件保持不变。

二手手机属性目录位于 `catalog/phone_properties.json`。自动填充只能使用精确名称和已审核的 `value_id`；平台出现新增、删除或改名时，先生成差异并更新目录，未审核的新值不得直接进入商品。
