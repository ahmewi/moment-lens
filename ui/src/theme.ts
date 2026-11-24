import { BrandVariants, createDarkTheme, createLightTheme, Theme } from "@fluentui/react-components";

// MomentLens brand ramp: indigo/blue, in keeping with the navy of the logo.
const momentLensBrand: BrandVariants = {
  10: "#020306",
  20: "#101427",
  30: "#1A2148",
  40: "#202C66",
  50: "#263685",
  60: "#2C40A5",
  70: "#3B55C5",
  80: "#5066D8",
  90: "#6577E1",
  100: "#7A89E9",
  110: "#8F9BF1",
  120: "#A4ADF7",
  130: "#B8BFFA",
  140: "#CCD1FC",
  150: "#DFE3FE",
  160: "#F0F2FF",
};

export const momentLensLightTheme: Theme = {
  ...createLightTheme(momentLensBrand),
};

// For the navy banner, so its tabs stay readable on a dark background.
export const momentLensDarkTheme: Theme = {
  ...createDarkTheme(momentLensBrand),
};
