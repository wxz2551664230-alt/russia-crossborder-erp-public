type Definition = {product_type:string,parentage:string,variation_themes?:string[]}

// NONE describes standalone listings and may deliberately omit variation themes.
export function variationState(definitions:Definition[], productType:string) {
  const parent=definitions.find(d=>d.product_type===productType&&d.parentage==='PARENT')
  const child=definitions.find(d=>d.product_type===productType&&d.parentage==='CHILD')
  const loaded=!!parent&&!!child
  const themes=loaded?(child.variation_themes||[]).filter(t=>parent.variation_themes?.includes(t)):[]
  return {loaded,themes}
}
