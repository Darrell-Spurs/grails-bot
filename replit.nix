{ pkgs }: {
  deps = [
    pkgs.opencv
    pkgs.libGL
    pkgs.mesa
    pkgs.libGLU
    pkgs.xorg.libX11
    pkgs.xorg.libXext
    pkgs.xorg.libXrender
  ];
}