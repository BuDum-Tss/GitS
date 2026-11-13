// Синтетические исходники для демо (ffmpeg): спокойный движущийся фон с таймкодом и тихий звук; закадр-заменитель.
// Файлы большие для git и в нём не хранятся (public/media и public/vo в .gitignore). Запуск: node demo/make-demo-media.mjs
import {spawnSync} from 'node:child_process';
import {existsSync, mkdirSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const font = join(root, 'public', 'fonts', 'IBMPlexMono-Regular.ttf');
mkdirSync(join(root, 'public', 'media'), {recursive: true});
mkdirSync(join(root, 'public', 'vo'), {recursive: true});

function ffmpeg(args) {
	const r = spawnSync('ffmpeg', ['-y', '-v', 'error', ...args], {cwd: root, encoding: 'utf8'});
	if (r.status !== 0) {
		console.error(r.stderr);
		process.exit(1);
	}
}

const sizes = {vertical: [1080, 1920], horizontal: [1920, 1080], square: [1080, 1080]};
for (const [kind, [w, h]] of Object.entries(sizes)) {
	const out = join('public', 'media', `demo_${kind}_h264.mp4`);
	if (existsSync(join(root, out)) && !process.argv.includes('--force')) continue;
	const size = Math.round(Math.min(w, h) / 9);
	ffmpeg([
		'-f', 'lavfi', '-i', `gradients=s=${w}x${h}:r=30:d=24:speed=0.012:c0=0x3b4654:c1=0x6b7a88:c2=0x2d3b4a:c3=0x52606d:nb_colors=4`,
		'-f', 'lavfi', '-i', 'sine=f=196:r=48000:d=24',
		'-f', 'lavfi', '-i', 'sine=f=247:r=48000:d=24',
		'-filter_complex',
		`[0:v]drawtext=fontfile=public/fonts/IBMPlexMono-Regular.ttf:text='%{pts\\:hms}':fontsize=${size}:fontcolor=white@0.55:x=(w-text_w)/2:y=(h-text_h)/2,format=yuv420p[v];[1:a][2:a]amix=inputs=2,volume=0.15[a]`,
		'-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-crf', '26', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '64k', '-r', '30', '-t', '24', out,
	]);
	console.log('создан', out);
}

// закадр-заменитель: тон 150 Гц с модуляцией 4 Гц, 3,4 с (настоящий закадр придёт из core.tts)
const vo = join('public', 'vo', 'vo1.wav');
if (!existsSync(join(root, vo)) || process.argv.includes('--force')) {
	ffmpeg(['-f', 'lavfi', '-i', 'sine=f=150:r=24000:d=3.4', '-af', 'tremolo=f=4:d=0.8,volume=0.8', '-ac', '1', vo]);
	console.log('создан', vo);
}
if (!existsSync(font)) console.warn('нет шрифта для таймкода:', font);
