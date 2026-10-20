export function previewStatus(response: { status?: string; issues?: { severity?: string }[] }, rejected = false): string {
  if (rejected || response.status === 'INVALID' || response.issues?.some(i => i.severity === 'ERROR')) return '需修正'
  return response.status === 'VALID' ? '通过（未发布）' : '未确认通过'
}

export function needsIdentifierReview(rows: { response?: { issues?: { code?: string; severity?: string }[] } }[]): boolean {
  return rows.some(row => row.response?.issues?.some(issue => issue.code === '8560' && issue.severity === 'ERROR'))
}

export const DEFAULT_KEYWORD_REFERENCE_MAX_CHARS = 20000

export function keywordReferenceLength(value: string): number {
  return Array.from(value).length
}

export function keywordReferenceError(value: string, limit: number): string {
  const length = keywordReferenceLength(value)
  return length > limit ? `本批关键词共 ${length} 个字符，最多 ${limit} 个，请缩短后再建立批次` : ''
}

export function amazonRequestError(error: { code?: string; message?: string; details?: unknown } | null | undefined,
  keywordLimit = DEFAULT_KEYWORD_REFERENCE_MAX_CHARS): string {
  if (error?.code !== 'validation_error' || !Array.isArray(error.details)) return error?.message || '操作未完成'
  const labels: Record<string, string> = {
    store_id: '目标店铺', items: '上架规格', product_id: '产品', variant_id: '规格', images: '上架图片',
    product_type: '商品类目', fee_category: '收费类目', pricing: '定价', parent_sku: '父 SKU',
    variation_theme: '变体主题', shipping_template: 'FBM 配送模板', quantity: '可售库存',
    handling_days: '处理时间', attributes: '商品属性', keywords: '本批关键词',
    merge_group_size: '每个链接合并数量', request_id: '批次请求编号', generation_provider: '资料准备方式',
    free_shipping_confirmed: '包邮确认', main_image_confirmed: '主图确认',
  }
  const messages = error.details.slice(0, 3).map((detail: any) => {
    const path = Array.isArray(detail?.loc) ? detail.loc.filter((part: unknown) => part !== 'body') : []
    const field = path[0]
    if (field === 'keywords' && detail.type === 'string_too_long') {
      const reportedLimit = /at most (\d+) characters/.exec(detail.msg || '')
      const limit = reportedLimit ? Number(reportedLimit[1]) : keywordLimit
      return limit === 200 ? '类目搜索关键词超过 200 个字符，请缩短后再搜索类目' : `本批关键词超过 ${limit} 个字符，请缩短后再建立批次`
    }
    let label = labels[field] || '提交数据'
    if (field === 'items' && typeof path[1] === 'number') label = `第 ${path[1] + 1} 个规格的${labels[path[2]] || '资料'}`
    if (detail.type === 'missing') return `请填写${label}`
    if (['quantity', 'handling_days', 'merge_group_size'].includes(field)) {
      const ranges: Record<string, string> = { quantity: '0–100000', handling_days: '1–30', merge_group_size: '2–200' }
      return `${label}必须是 ${ranges[field]} 范围内的整数`
    }
    if (detail.type === 'string_pattern_mismatch') return `${label}格式不正确，请重新选择或填写`
    if (detail.type === 'extra_forbidden') return '提交数据包含当前版本不支持的字段，请保留填写内容后更新页面'
    return `${label}：${detail.msg || '数据格式不正确，请检查填写值'}`
  })
  return messages.join('；') || error.message || '提交的数据不完整或格式不正确'
}
