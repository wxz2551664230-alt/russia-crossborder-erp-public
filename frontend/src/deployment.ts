import { ref } from 'vue'
import type { CatalogAssetLink } from './types'

export const sharedProductsPilot = ref(false)
export const sharedProductPages = ['/catalog/amazon/publish','/users', '/configuration', '/stores', '/connections', '/settings', '/ad-optimization', '/ad-optimization/skus', '/orders/pod', '/catalog/wb/publish', '/catalog/wb/processing', '/catalog/wb/pending-stock', '/catalog/wb/pending-price', '/catalog/wb/completed', '/catalog/wb/exceptions', '/catalog/ozon/publish', '/catalog/ozon/image-review', '/catalog/ozon/pending-submit', '/catalog/ozon/pending-stock', '/catalog/ozon/pending-promotion', '/catalog/ozon/completed', '/catalog/products', '/catalog/follow-products', '/self-listing/products', '/self-listing/import', '/pod/products', '/pod/studio', '/pod/artworks', '/pod/ai-design', '/pod/supplier-gallery', '/pod/us-catalog', '/ozon-follow/products', '/ozon-follow/details', '/execution-devices', '/jobs', '/codex-takeover', '/client-download']

export function safeProductUrl(value?:string): string {
  try { const url=new URL(value || ''); return ['https:','http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '' } catch { return '' }
}
export function catalogImageUrl(link?:CatalogAssetLink): string {
  if (!link || link.asset.mirror_status === 'deleted') return ''
  try { const url=new URL(link.asset.oss_url); return url.protocol==='https:' && url.hostname==='example-assets.oss-cn-chengdu.aliyuncs.com' && !url.username && !url.password ? url.href : '' } catch { return '' }
}
