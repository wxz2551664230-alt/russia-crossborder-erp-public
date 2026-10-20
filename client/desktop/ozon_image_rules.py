"""Expose the original Ozon poster builder; no generation or platform requests."""
import re

def poster_content(content,store_index=0):
    from erp.modules.ozon_follow.legacy.services.ai_listing import store_creative_profile
    lines=content.get('poster_text_lines_ru')
    if not isinstance(lines,list) or not 1<=len(lines)<=4:
        raise ValueError('主图必须提供1到4条真实俄文海报短句：标题及事实卖点')
    if any(not isinstance(line,str) or not line.strip() or len(line)>68 or not re.search('[А-Яа-яЁё]',line) or re.search('[\u3400-\u9fff]',line) for line in lines):
        raise ValueError('海报短句必须是清晰俄文，每条最多68字符，不能包含中文')
    direction=content.get('creative_direction_en')
    if not isinstance(direction,str) or not direction.strip():raise ValueError('缺少商业海报构图说明 creative_direction_en')
    return {**content,'poster_text_lines_ru':[line.strip() for line in lines],'store_creative_profile':store_creative_profile(store_index)}

def main_prompt(content,store_index=0):
    from erp.modules.ozon_follow.legacy.services.ai_listing import build_main_image_prompt
    return build_main_image_prompt(poster_content(content,store_index))

def export_rules(snapshot):
    from erp.modules.ozon_follow.legacy.services.ai_listing import store_creative_profile,build_main_image_prompt
    profile=store_creative_profile(snapshot.get('store_index',0))
    return {'version':1,'aspect_ratio':'3:4','example_sizes':[[900,1200],[1200,1600]],
            'main_style':'俄文商业海报：明确标题、真实卖点、产品主体及有层次的背景；不能只生成场景照片',
            'product_frame_occupancy':'50–72%','poster_text_lines_ru':'1–4条真实俄文短句，每条最多68字符；只能渲染这些文字',
            'store_creative_profile':profile,'rule_source':'原程序 ai_listing.build_main_image_prompt',
            'main_prompt_template':build_main_image_prompt({'poster_text_lines_ru':['ЗАМЕНИТЬ: заголовок товара','ЗАМЕНИТЬ: подтверждённый факт'],'store_creative_profile':profile}),
            'instructions':'先填真实 poster_text_lines_ru 和 creative_direction_en，再执行 image-prompt 命令取得原函数的完整提示词。模板占位文字不可出现在生成图。用完整提示词及实际参考图调用 imagegen；不符合比例要重新生成，不能拉伸商品。图片策略 reuse 不生成。'}

def validate_image(row,size,poster_lines=None,aspect_ratio='3:4'):
    width,height=size
    if aspect_ratio == '1:1':
        if width<=0 or height<=0 or width!=height:
            raise ValueError(f'本批次Ozon生成图必须为1:1方图，当前{width}×{height}；请重新生成合适比例')
    elif width<=0 or height<=0 or abs(4*width-3*height)>3:
        raise ValueError(f'Ozon生成图必须为3:4竖版，当前{width}×{height}；请重新生成合适比例')
    if poster_lines is not None:
        if row.get('poster_layout_verified') is not True or row.get('russian_text_verified') is not True or row.get('visible_text_lines_ru')!=poster_lines:
            raise ValueError('请实际核对海报布局及俄文文字，并记录与白名单完全一致的 visible_text_lines_ru')
