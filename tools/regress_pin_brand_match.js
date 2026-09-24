// 回归测试：_pin_and_locate_model_box 的 Method B0 品牌值匹配逻辑（修复后）
// 验证点：
//  1) 联合串品牌框 'Huawei/华为' / 'Redmi/红米' / 'Samsung/三星' / 'Xiaomi/小米' 能被命中（修复前仅精确匹配会漏）
//  2) 含空格的型号值（如 '三星 W2018 典藏尊铂版'）绝不被误认成品牌候选（沿用第1次修复）
//  3) 真实型号值（如 'Pura90 Pro Max'）不被误判为品牌
//  4) 纯品牌名/英文（'OPPO' / 'Huawei'）仍命中（向后兼容）
//
// 用法：node tools/regress_pin_brand_match.js

// 与 test_doudian_batch.py 中 Method B0 完全一致的匹配逻辑
function isBrandBox(text, brandCn, brandEn) {
  if (!text) return false;
  const lc = text.toLowerCase();
  const isBrand =
    (brandCn && (text === brandCn || text.includes(brandCn))) ||
    (brandEn && (lc === brandEn.toLowerCase() || lc.includes(brandEn.toLowerCase())));
  // 含空格者必为型号值（品牌值无空格），排除以免三星类双框误判
  return isBrand && !/\s/.test(text);
}

// 旧逻辑（修复前，仅精确匹配）——用于对比，证明修复前会漏掉联合串
function isBrandBoxOld(text, brandCn, brandEn) {
  if (!text) return false;
  return text === brandCn || (brandEn && text.toLowerCase() === brandEn.toLowerCase());
}

const cases = [
  // [text, brandCn, brandEn, expectNew, 说明]
  ['Huawei/华为', '华为', 'Huawei', true, '华为联合串 -> 命中(本次修复核心)'],
  ['Redmi/红米', '红米', 'Redmi', true, '红米联合串 -> 命中'],
  ['Samsung/三星', '三星', 'Samsung', true, '三星联合串 -> 命中(品牌,非型号)'],
  ['Xiaomi/小米', '小米', 'Xiaomi', true, '小米联合串 -> 命中'],
  ['OPPO', 'OPPO', 'OPPO', true, '纯品牌名 -> 命中(向后兼容)'],
  ['Huawei', '华为', 'Huawei', true, '纯英文品牌 -> 命中(向后兼容)'],
  ['华为', '华为', 'Huawei', true, '纯中文品牌 -> 命中(向后兼容)'],

  ['三星 W2018 典藏尊铂版', '三星', 'Samsung', false, '含空格型号值 -> 排除(第1次修复)'],
  ['Pura90 Pro Max', '华为', 'Huawei', false, '真实型号值 -> 不误判为品牌'],
  ['Galaxy Z Fold7', '三星', 'Samsung', false, '三星机型型号值(含空格) -> 排除'],
  ['iPhone 15 Pro', '苹果', 'Apple', false, '苹果型号值(含空格) -> 排除'],
];

let pass = 0, fail = 0;
console.log('=== 新逻辑 (isBrandBox) ===');
for (const [text, cn, en, expect, desc] of cases) {
  const got = isBrandBox(text, cn, en);
  const ok = got === expect;
  if (ok) pass++; else fail++;
  console.log(
    `${ok ? '✅' : '❌'} expect=${expect ? '命中' : '排除'} got=${got ? '命中' : '排除'} | ${text} @(${cn}/${en}) — ${desc}`
  );
}

// 对比：旧逻辑在联合串上必然漏
console.log('\n=== 旧逻辑 (isBrandBoxOld) 对比（应漏掉联合串） ===');
for (const [text, cn, en, expectNew, desc] of cases.slice(0, 4)) {
  const gotOld = isBrandBoxOld(text, cn, en);
  console.log(`${gotOld ? '✅命中' : '⚠️漏掉'} | ${text} @(${cn}/${en})`);
}

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail === 0 ? 0 : 1);
