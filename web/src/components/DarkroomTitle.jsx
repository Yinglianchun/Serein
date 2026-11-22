import { useId } from "react";

export function DarkroomTitle() {
  const id = useId();
  return (
    <div className="darkroom-title-art" aria-hidden="true">
      <div className="darkroom-title-art__poster">
        {/* 表盘、轨道、邮戳和边框 */}
        <svg className="L back" viewBox="0 0 100 92" preserveAspectRatio="xMidYMid meet">
          <defs>
            <pattern
              id={`${id}-hat`}
              width="1.1"
              height="1.1"
              patternUnits="userSpaceOnUse"
              patternTransform="rotate(45)"
            >
              <line x1="0" y1="0" x2="0" y2="1.1" stroke="#fff" strokeWidth=".32" />
            </pattern>
            <radialGradient id={`${id}-glg`}>
              <stop offset="0" stopColor="#f0d69c" stopOpacity=".95" />
              <stop offset=".35" stopColor="#f0d69c" stopOpacity=".34" />
              <stop offset="1" stopColor="#f0d69c" stopOpacity="0" />
            </radialGradient>
            <radialGradient id={`${id}-gl`}>
              <stop offset="0" stopColor="#fff" stopOpacity=".85" />
              <stop offset=".35" stopColor="#fff" stopOpacity=".28" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </radialGradient>
            <path id={`${id}-sp`} d="M0,-2 Q.25,-.25 2,0 Q.25,.25 0,2 Q-.25,.25 -2,0 Q-.25,-.25 0,-2Z" />
            <path id={`${id}-pm`} d="M78,6.2 a6.8,6.8 0 1,1 -.01,0" />
          </defs>

          {/* 表盘：藏在「慢」后面，指针慢到几乎看不出在走 */}
          <g className="fd" style={{ "--dl": "1.2s", "--o": ".9" }}>
            <circle cx="42" cy="62" r="30" strokeOpacity=".2" />
            <circle
              cx="42"
              cy="62"
              r="28"
              strokeWidth="1.6"
              strokeOpacity=".22"
              strokeDasharray=".16 2.77"
              className="spin"
              style={{ transformOrigin: "42px 62px" }}
            />
            <g className="hand" style={{ transformOrigin: "42px 62px" }} strokeOpacity=".4">
              <path d="M42,62 V36" />
              <circle cx="42" cy="35" r=".7" />
            </g>
            <g
              className="hand"
              style={{ transformOrigin: "42px 62px", animationDuration: "43200s" }}
              strokeOpacity=".3"
            >
              <path d="M42,62 V46" />
            </g>
          </g>
          {/* 轨道：空间 */}
          <g transform="rotate(-18 52 46)" className="fd" style={{ "--dl": "1.6s", "--o": "1" }}>
            <path
              d="M6,46 a46,12 0 1,0 92,0 a46,12 0 1,0 -92,0"
              stroke="rgb(240,214,156)"
              strokeOpacity=".5"
              strokeDasharray="1.2 1.6"
            />
            <circle className="orbit-dot" r=".85" fill="rgb(240,214,156)" stroke="none">
              <animateMotion dur="34s" repeatCount="indefinite">
                <mpath href={`#${id}-pm0`} />
              </animateMotion>
            </circle>
            <path id={`${id}-pm0`} d="M6,46 a46,12 0 1,0 92,0 a46,12 0 1,0 -92,0" stroke="none" />
          </g>
          {/* 邮戳：慢递 */}
          <g className="fd" style={{ "--dl": "2.2s", "--o": ".34" }}>
            <g className="stamp" style={{ transformOrigin: "78px 13px" }}>
              <circle cx="78" cy="13" r="9.4" strokeWidth=".2" />
              <circle cx="78" cy="13" r="4.6" strokeWidth=".2" />
              <text className="tx" fontSize="2.15" style={{ letterSpacing: ".45px" }}>
                <textPath href={`#${id}-pm`}>SLOW POST · TIME · SPACE · SLOW POST ·</textPath>
              </text>
            </g>
          </g>
          {/* 边框：偏移的两个方框 */}
          <rect
            className="dr"
            pathLength="1"
            x="23"
            y="46"
            width="37"
            height="35"
            strokeOpacity=".34"
            style={{ "--dl": "1.9s", "--dur": "2.4s" }}
          />
          <rect
            className="fd"
            x="25.2"
            y="48.2"
            width="37"
            height="35"
            fill="#fff"
            fillOpacity=".045"
            stroke="none"
            style={{ "--dl": "2.4s" }}
          />
        </svg>

        {/* 四个字 */}
        <div className="pos" style={{ "--x": "26", "--y": "25", "--S": "26" }}>
          <svg className="hg" viewBox="0 0 10 22" aria-hidden="true">
            <defs>
              <clipPath id={`${id}-hT`}>
                <path d="M1.6,1 H8.4 C8.4,8 5,10 5,11 C5,10 1.6,8 1.6,1Z" />
              </clipPath>
              <clipPath id={`${id}-hB`}>
                <path d="M5,11 C5,12 8.4,14 8.4,21 H1.6 C1.6,14 5,12 5,11Z" />
              </clipPath>
            </defs>
            <g className="hgf">
              <path d="M.6,1 H9.4 M.6,21 H9.4" strokeWidth=".8" />
              <path
                d="M1.6,1 C1.6,8 5,10 5,11 C5,12 1.6,14 1.6,21 M8.4,1 C8.4,8 5,10 5,11 C5,12 8.4,14 8.4,21"
                strokeWidth=".7"
              />
              <g clipPath={`url(#${id}-hT)`}>
                <rect
                  className="sT"
                  x="0"
                  y="1"
                  width="10"
                  height="10"
                  fill="#fff"
                  fillOpacity=".8"
                  stroke="none"
                />
              </g>
              <g clipPath={`url(#${id}-hB)`}>
                <rect
                  className="sB"
                  x="0"
                  y="11"
                  width="10"
                  height="10"
                  fill="#fff"
                  fillOpacity=".8"
                  stroke="none"
                />
              </g>
              <g className="strm">
                <circle
                  className="gr"
                  style={{ "--g": "0s" }}
                  cx="5"
                  cy="11.8"
                  r=".42"
                  fill="#fff"
                  stroke="none"
                />
                <circle
                  className="gr"
                  style={{ "--g": ".55s" }}
                  cx="5"
                  cy="11.8"
                  r=".42"
                  fill="#fff"
                  stroke="none"
                />
                <circle
                  className="gr"
                  style={{ "--g": "1.1s" }}
                  cx="5"
                  cy="11.8"
                  r=".42"
                  fill="#fff"
                  stroke="none"
                />
              </g>
            </g>
          </svg>
          <span className="gh k" style={{ "--d": ".3s" }}>
            寸
          </span>
          <span className="c g f up k" style={{ "--d": ".3s" }}>
            寸
          </span>
          <span className="c g f lo k" style={{ "--d": ".3s", translate: ".14em .01em" }}>
            寸
          </span>
        </div>
        <div className="pos" style={{ "--x": "51", "--y": "22", "--S": "22" }}>
          <span className="gh" style={{ "--d": ".8s", "--gx": ".06em", "--gy": ".05em" }}>
            空
          </span>
          <span className="c h" style={{ "--d": ".8s" }}>
            空
          </span>
        </div>
        <div className="pos" style={{ "--x": "42", "--y": "63", "--S": "34" }}>
          <span
            className="gh e"
            style={{ "--d": "1.3s", "--gx": ".26em", "--gy": ".02em", "--go": ".45" }}
          >
            慢
          </span>
          <span
            className="gh e"
            style={{ "--d": "1.3s", "--gx": ".14em", "--gy": ".01em", "--go": ".8" }}
          >
            慢
          </span>
          <span className="c" style={{ "--d": "1.3s" }}>
            慢
          </span>
        </div>
        <div className="pos" style={{ "--x": "71", "--y": "66", "--S": "28" }}>
          <span className="gh" style={{ "--d": "1.9s", "--gx": "-.05em", "--gy": ".05em" }}>
            递
          </span>
          <span className="c g2 f2" style={{ "--d": "1.9s" }}>
            递
          </span>
        </div>

        {/* 星芒、阴影和标注 */}
        <svg className="L front" viewBox="0 0 100 92" preserveAspectRatio="xMidYMid meet">
          {/* 右下角的金色星芒 */}
          <g className="fd" style={{ "--dl": "4s" }}>
            <g transform="translate(90 78) scale(1.3)">
              <circle
                className="halo"
                r="5.6"
                fill={`url(#${id}-glg)`}
                stroke="none"
                style={{ "--dl": ".4s" }}
              />
              <use
                className="tw"
                href={`#${id}-sp`}
                fill="#fff3d6"
                stroke="none"
                style={{ "--dl": ".4s" }}
              />
            </g>
          </g>
          {/* 斜线阴影块（参考字幕里的那块） */}
          <rect
            className="fd"
            x="47"
            y="49.5"
            width="8.4"
            height="6.2"
            fill={`url(#${id}-hat)`}
            stroke="none"
            style={{ "--dl": "2.8s", "--o": ".6" }}
          />
          {/* 四角 */}
          <path
            className="dr"
            pathLength="1"
            d="M8,8 h4.2 M8,8 v4.2"
            strokeOpacity=".7"
            style={{ "--dl": ".4s", "--dur": "1.2s" }}
          />
          <path
            className="dr"
            pathLength="1"
            d="M95,85 h-4.2 M95,85 v-4.2"
            strokeOpacity=".7"
            style={{ "--dl": "3.6s", "--dur": "1.2s" }}
          />
          <text
            className="tx fd"
            x="14"
            y="6.3"
            fontSize="1.9"
            style={{ letterSpacing: ".9px", "--dl": ".8s", "--o": ".5" }}
          >
            TIME · SPACE
          </text>
          {/* 花体：夹在两行之间 */}
          <text
            className="tx fd"
            x="30"
            y="42.6"
            fontSize="5.4"
            transform="rotate(-3 30 42.6)"
            style={{ fontFamily: "var(--script)", "--dl": "3.2s", "--o": ".78" }}
          >
            across time &amp; space
          </text>
          {/* 拼音 */}
          <text
            className="tx fd"
            x="8"
            y="88.4"
            fontSize="2.3"
            style={{ letterSpacing: ".95px", "--dl": "3.4s", "--o": ".55" }}
          >
            # shí kōng màn dì
          </text>
          <path
            className="dr"
            pathLength="1"
            d="M8,90.6 h13"
            strokeOpacity=".3"
            style={{ "--dl": "3.6s", "--dur": "1.4s" }}
          />
          {/* 在途：一个点用 15 分钟走完这条线 */}
          <text
            className="tx fd"
            x="62"
            y="84.6"
            fontSize="1.7"
            style={{ letterSpacing: ".6px", "--dl": "4s", "--o": ".5" }}
          >
            IN TRANSIT · ETA ∞
          </text>
          <path
            className="dr"
            pathLength="1"
            d="M62,87 H96"
            strokeOpacity=".3"
            style={{ "--dl": "3.8s", "--dur": "1.6s" }}
          />
          <g className="fd" style={{ "--dl": "4.4s" }}>
            <circle cx="62" cy="87" r=".5" fill="#fff" stroke="none" />
            <circle cx="96" cy="87" r=".5" strokeWidth=".2" />
            <circle className="crawl" cx="62.8" cy="87" r=".75" fill="#fff" stroke="none" />
          </g>
          <text
            className="tx fd"
            x="62"
            y="90.8"
            fontSize="2"
            style={{ letterSpacing: ".8px", "--dl": "4.6s", "--o": ".5" }}
          >
            寄往 · 尚未抵达的明天
          </text>
          {/* 星芒：原地呼吸，变大时亮起一圈光晕 */}
          <g transform="translate(9.5 25)">
            <circle
              className="halo"
              r="4.4"
              fill={`url(#${id}-gl)`}
              stroke="none"
              style={{ "--dl": ".2s" }}
            />
            <use className="tw" href={`#${id}-sp`} fill="#fff" stroke="none" style={{ "--dl": ".2s" }} />
          </g>
          <g transform="translate(90 45)">
            <circle
              className="halo"
              r="4.4"
              fill={`url(#${id}-gl)`}
              stroke="none"
              style={{ "--dl": "1.9s" }}
            />
            <use
              className="tw"
              href={`#${id}-sp`}
              fill="#fff"
              stroke="none"
              style={{ "--dl": "1.9s" }}
            />
          </g>
          <g transform="translate(31 83)">
            <circle
              className="halo"
              r="4.4"
              fill={`url(#${id}-gl)`}
              stroke="none"
              style={{ "--dl": "3.6s" }}
            />
            <use
              className="tw"
              href={`#${id}-sp`}
              fill="#fff"
              stroke="none"
              style={{ "--dl": "3.6s" }}
            />
          </g>
        </svg>
      </div>
    </div>
  );
}
