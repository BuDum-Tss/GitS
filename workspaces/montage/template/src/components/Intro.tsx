import {Video} from '@remotion/media';
import React from 'react';
import {AbsoluteFill, staticFile, useVideoConfig} from 'remotion';
import type {InsertOf} from '../schema';
import {colors} from '../style/tokens';

export const Intro: React.FC<{insert?: InsertOf<'intro'>; durationInFrames?: number}> = () => {
    const {width, height} = useVideoConfig();
    const suffix = height > width ? 'vertical_light' : 'light';
    return (
        <AbsoluteFill style={{backgroundColor: colors['sign-paper']}}>
            <Video src={staticFile(`intro/Aperture_Boot_${suffix}.mp4`)} muted
                style={{width: '100%', height: '100%', objectFit: 'contain'}} />
        </AbsoluteFill>
    );
};
