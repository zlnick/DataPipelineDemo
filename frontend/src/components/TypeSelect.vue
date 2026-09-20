<template>
  <!-- 类型选择器：未实现的类型（预留）渲染为禁用态 + Tooltip 提示 -->
  <el-select
    :model-value="modelValue"
    :placeholder="placeholder || t('common.selectType')"
    style="width: 100%"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <el-option
      v-for="opt in options"
      :key="opt.value"
      :label="optLabel(opt) + (opt.enabled ? '' : t('common.reservedShort'))"
      :value="opt.value"
      :disabled="!opt.enabled"
    >
      <el-tooltip :content="optDesc(opt)" :disabled="opt.enabled" placement="left">
        <span>{{ optLabel(opt) }}{{ opt.enabled ? '' : t('common.reservedLong') }}</span>
      </el-tooltip>
    </el-option>
  </el-select>
</template>

<script setup>
import { useI18n } from 'vue-i18n'

// props: options（类型枚举数组，含 enabled 标记）、modelValue、placeholder
defineProps({
  options: { type: Array, required: true },
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '' },
})
defineEmits(['update:modelValue'])

const { t, locale } = useI18n()
// 英文页用常量自带的 labelEn / descEn（前端常量，非后端接口）
const optLabel = (o) => (locale.value === 'en' && o.labelEn ? o.labelEn : o.label)
const optDesc = (o) => (locale.value === 'en' && o.descEn ? o.descEn : o.desc)
</script>
