export function buildVariantSelection(products:any[], previous:any[]=[]):any[]{
  const saved=new Map(previous.map(row=>[row.variant.id,row]))
  return products.flatMap(product=>product.variants.filter((v:any)=>v.active&&v.available!==false).map((variant:any)=>{
    const old=saved.get(variant.id)
    return old?{...old,product,variant}:{product,variant,checked:true,
      images:[...new Set<string>(product.assets.filter((a:any)=>a.is_current&&!['source','artwork','print','production'].includes(a.role)&&(!a.variant_id||a.variant_id===variant.id)&&a.asset.mirror_status!=='deleted').map((a:any)=>a.asset.oss_url).filter((u:string)=>u?.startsWith('https://example-assets.oss-cn-chengdu.aliyuncs.com/')))].slice(0,10)}
  }))
}

export function selectionSummary(rows:any[], merge:boolean, size:number){
  const checked=rows.filter(row=>row.checked)
  if(!merge)return {count:checked.length,links:checked.length,batches:Math.ceil(checked.length/200)}
  const buckets=new Map<string,number>()
  for(const row of checked){const key=`${row.product.production?.supplier}:${row.product.production?.product_type_id}`;buckets.set(key,(buckets.get(key)||0)+1)}
  const links=[...buckets.values()].reduce((total,n)=>total+Math.ceil(n/size),0)
  return {count:checked.length,links,batches:links}
}

export type SharedImageRule={key:string;url:string;action:'main'|'additional'|'exclude'}
const normalized=(value:any)=>String(value||'').normalize('NFKC').trim().toLowerCase()
export function variantKey(variant:any){
  const size=normalized(variant.size),color=normalized(variant.color)
  return size||color?`${size}|${color}`:`spec|${normalized(variant.specification)}`
}
export function sampleRow(rows:any[],keys:string[]){
  const ordered=[...rows].sort((a,b)=>String(b.variant.sku||'').localeCompare(String(a.variant.sku||'')))
  return ordered.find(row=>keys.includes(variantKey(row.variant)))||ordered[0]
}
export function sharedVariantOptions(rows:any[]){
  const sample=sampleRow(rows,[])
  if(!sample)return []
  const products=new Set(rows.map(row=>row.product.id))
  const options=new Map<string,any>()
  for(const variant of sample.product.variants.filter((v:any)=>v.active!==false)){
    const key=variantKey(variant)
    if(!options.has(key))options.set(key,{key,label:variant.size||variant.specification||'单一规格',
      color:variant.color||'默认颜色',matched:new Set(rows.filter(row=>variantKey(row.variant)===key).map(row=>row.product.id)).size,total:products.size})
  }
  return [...options.values()]
}
function imageKey(link:any){
  const value=String(link.asset.name||link.asset.oss_url?.split('?')[0]||'').split(/[\\/]/).pop()||''
  try{return normalized(decodeURIComponent(value))}catch{return normalized(value)}
}
function imageChoices(row:any){
  const links=(row.product.assets||[]).filter((a:any)=>a.is_current&&!['source','artwork','print','production'].includes(a.role)
    &&(!a.variant_id||a.variant_id===row.variant.id)&&a.asset.mirror_status!=='deleted'
    &&a.asset.oss_url?.startsWith('https://example-assets.oss-cn-chengdu.aliyuncs.com/'))
  const choices=new Map<string,{key:string;url:string;ambiguous:boolean}>()
  for(const link of links){
    const key=imageKey(link)
    // A specification's own picture takes precedence over the shared picture.
    if(!link.variant_id&&links.some((other:any)=>other.variant_id===row.variant.id&&imageKey(other)===key))continue
    const prior=choices.get(key),url=link.asset.oss_url
    choices.set(key,{key,url,ambiguous:!!prior&&(prior.ambiguous||prior.url!==url)})
  }
  return [...choices.values()]
}
export function syncImageRules(row:any,previous:SharedImageRule[]=[]):SharedImageRule[]{
  if(!row)return []
  const choices=imageChoices(row),byKey=new Map(choices.map(choice=>[choice.key,choice]))
  if(!previous.length)return choices.slice(0,10).map((choice,index)=>({...choice,action:index===0?'main':'additional'}))
  // Keep the policy when another page or sample is selected, including missing
  // names. A missing enabled picture must be resolved, never silently replaced.
  return [...previous.map(rule=>({...rule,url:byKey.get(rule.key)?.url||''})),
    ...choices.filter(choice=>!previous.some(rule=>rule.key===choice.key)).map(choice=>({...choice,action:'exclude' as const}))]
}
export function applySharedSelection(rows:any[],keys:string[],rules:SharedImageRule[]){
  const chosen=rows.filter(row=>keys.includes(variantKey(row.variant)))
  const enabled=[...rules.filter(rule=>rule.action==='main'),...rules.filter(rule=>rule.action==='additional')]
  const issues:string[]=[]
  if(chosen.length&&new Set(rows.map(row=>`${row.product.production?.supplier}:${row.product.production?.product_type_id}`)).size>1)
    issues.push('本批请选择同一基础商品的产品，再统一设置规格与图片。')
  if(chosen.length&&(rules.filter(rule=>rule.action==='main').length!==1||enabled.length>10))
    issues.push('请设置一张主图，启用图片总数不能超过10张。')
  const applied=chosen.map(row=>{
    const available=new Map(imageChoices(row).map(choice=>[choice.key,choice])),images:string[]=[]
    for(const rule of enabled){
      const match=available.get(rule.key)
      if(!match||match.ambiguous)issues.push(`${row.variant.sku}：${rule.key} ${match?'有多张同名图片':'缺少对应展示图'}`)
      else images.push(match.url)
    }
    return {...row,checked:true,images}
  })
  return {rows:applied,issues,unmatchedProducts:new Set(rows.map(row=>row.product.id)).size-new Set(chosen.map(row=>row.product.id)).size}
}
