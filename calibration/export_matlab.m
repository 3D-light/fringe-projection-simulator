function export_matlab(camera_file, projector_file, output_file, projector_size)
%EXPORT_MATLAB Write the camera-projector calibration JSON used by Fringe Lab.
% Example:
%   export_matlab("cameraParams.mat","ProjectionParams.mat","system.json",[1080 1920])
% The projector size is [height width]. The source files must use the same
% calibration board poses in the same order, and millimetres for translation.
    arguments
        camera_file (1,1) string
        projector_file (1,1) string
        output_file (1,1) string
        projector_size (1,2) double {mustBePositive,mustBeInteger}
    end
    c = load(camera_file, "cameraParams");
    p = load(projector_file, "ProjectionParams");
    camera = c.cameraParams;
    projector = p.ProjectionParams;
    assert(size(camera.RotationMatrices,3)==size(projector.RotationMatrices,3), ...
        "Camera and projector must contain a matching set of paired poses.")
    data = struct();
    data.worldUnits = camera.WorldUnits;
    data.projectorWorldUnits = projector.WorldUnits;
    data.cameraK = camera.IntrinsicMatrix';
    data.projectorK = projector.IntrinsicMatrix';
    data.cameraRadial = camera.RadialDistortion;
    data.projectorRadial = projector.RadialDistortion;
    data.cameraTangential = camera.TangentialDistortion;
    data.projectorTangential = projector.TangentialDistortion;
    data.cameraImageSize = camera.ImageSize;
    data.projectorImageSize = projector_size;
    data.cameraRotationMatrices = permute(camera.RotationMatrices,[2 1 3]);
    data.projectorRotationMatrices = permute(projector.RotationMatrices,[2 1 3]);
    data.cameraTranslationVectors = camera.TranslationVectors;
    data.projectorTranslationVectors = projector.TranslationVectors;
    data.coordinateConvention = "1-based pixels; world-to-sensor column vectors; pose index 1";
    fid = fopen(output_file,"w","n","UTF-8");
    assert(fid>=0,"Failed to open the calibration output file.")
    cleanup = onCleanup(@() fclose(fid));
    fprintf(fid,"%s\n",jsonencode(data,PrettyPrint=true));
end
