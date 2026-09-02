<template>
  <!-- 类型选择器：未实现的类型（预留）渲染为禁用态 + Tooltip 提示 -->
  <el-select
    :model-value="modelValue"
    :placeholder="placeholder || '请选择类型'"
    style="width: 100%"
    @update:model-value="(v) => $emit('update:modelValue', v)"
  >
    <el-option
      v-for="opt in options"
      :key="opt.value"
      :label="opt.label + (opt.enabled ? '' : '（预留）')"
      :value="opt.value"
      :disabled="!opt.enabled"
    >
      <el-tooltip :content="opt.desc" :disabled="opt.enabled" placement="left">
        <span>{{ opt.label }}{{ opt.enabled ? '' : '（预留，暂未实现）' }}</span>
      </el-tooltip>
    </el-option>
  </el-select>
</template>

<script setup>
// props: options（类型枚举数组，含 enabled 标记）、modelValue、placeholder
defineProps({
  options: { type: Array, required: true },
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '' },
})
defineEmits(['update:modelValue'])
</script>
