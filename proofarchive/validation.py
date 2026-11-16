import math
import re
from image_lab.validation import Invalid, Conflict


def text(value, name, limit=160, empty=False):
    if not isinstance(value, str):
        raise Invalid(name + '需要文本')
    value = value.strip()
    if (not value and not empty) or len(value) > limit:
        raise Invalid(name + '为空或过长')
    return value


def number(value, name, low, high):
    if isinstance(value, bool):
        raise Invalid(name + '需要数值')
    try:
        value = float(value)
    except (TypeError, ValueError) as error:
        raise Invalid(name + '需要数值') from error
    if not math.isfinite(value) or not low <= value <= high:
        raise Invalid(name + '超出范围')
    return value


def integer(value, name, low, high):
    result = number(value, name, low, high)
    if result != int(result):
        raise Invalid(name + '需要整数')
    return int(result)


def choice(value, name, values):
    if value not in values:
        raise Invalid(name + '不支持此值')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{24}', value):
        raise Invalid('档案对象编号无效')
    return value


def rectangle(value):
    if not isinstance(value, list) or len(value) != 4:
        raise Invalid('区域需要四个归一化坐标')
    result = [number(x, '区域坐标', 0, 1) for x in value]
    if result[0] >= result[2] or result[1] >= result[3]:
        raise Invalid('区域需要正宽和正高')
    return result


def object_value(value):
    if not isinstance(value, dict):
        raise Invalid('需要JSON对象')
    return value


def list_value(value, maximum=200):
    if not isinstance(value, list) or len(value) > maximum:
        raise Invalid('列表格式或数量无效')
    return value
