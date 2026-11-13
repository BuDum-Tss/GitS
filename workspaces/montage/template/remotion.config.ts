import {Config} from '@remotion/cli/config';

// Общие настройки рендера: H.264 и yuv420p (воспроизводится везде), перезапись результата без вопроса.
Config.setVideoImageFormat('jpeg');
Config.setPixelFormat('yuv420p');
Config.setCodec('h264');
Config.setOverwriteOutput(true);
