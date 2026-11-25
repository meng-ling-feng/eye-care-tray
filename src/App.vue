<template>
  <div class="page">
    <div class="panel">
      <div class="slider-head">
        <span class="slider-title">护眼强度</span>
        <span class="slider-pct">{{ percent }}%</span>
      </div>

      <div class="magic-track">
        <div class="knob" :style="knobStyle"></div>
        <input
          class="range-input"
          type="range"
          min="0"
          max="100"
          step="1"
          v-model.number="percent"
          @input="save"
          aria-label="护眼强度"
        />
      </div>

      <div class="track-labels">
        <span>正常</span>
        <span>最暖</span>
      </div>

      <label class="autostart-row">
        <span>开机自启动</span>
        <input class="switch" type="checkbox" v-model="autostart" @change="toggleAutostart" />
      </label>

      <div class="foot-tip">拖动滑块调节屏幕色温，退出程序时自动还原。</div>
    </div>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';

const api = window.electronAPI;

// 0% = 6500K 正常色温，100% = 3400K 最暖
const percent = ref(50);
const autostart = ref(false);
const toast = ref('');
let toastTimer = null;

const knobStyle = computed(() => ({
  left: `calc(19px + (100% - 38px) * ${percent.value / 100})`,
}));

function showToast(text) {
  toast.value = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.value = ''; }, 2600);
}

function save() {
  api.setConfig({ percent: percent.value });
}

function toggleAutostart() {
  api.setAutostart(autostart.value);
}

onMounted(async () => {
  const saved = await api.getConfig();
  if (saved && typeof saved.percent === 'number') {
    percent.value = saved.percent;
  }
  autostart.value = await api.getAutostart();

  api.onConfigChanged((next) => {
    if (next && typeof next.percent === 'number') {
      percent.value = next.percent;
    }
  });
  api.onGammaStatus((ok) => {
    if (!ok) showToast('屏幕色彩调节失败，请拖动滑块重试');
  });
});
</script>
