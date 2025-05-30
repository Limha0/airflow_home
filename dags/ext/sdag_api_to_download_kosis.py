import logging
import urllib3

from pendulum import datetime, from_format,now
from airflow.decorators import dag, task
from util.common_util import CommonUtil
from dto.tn_data_bsc_info import TnDataBscInfo
from dto.th_data_clct_mastr_log import ThDataClctMastrLog
from dto.tn_clct_file_info import TnClctFileInfo
from dto.tc_com_dtl_cd import TcCmmnDtlCd as CONST
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.sftp.operators.sftp import SFTPHook
from sqlalchemy.orm import sessionmaker
from airflow.exceptions import AirflowSkipException
from util.file_util import FileUtil
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@dag(
    dag_id="sdag_api_to_download_kosis",
    schedule="5 0 7,21 * *",
    start_date=datetime(2023, 9, 16, tz="Asia/Seoul"),  # UI 에 KST 시간으로 표출하기 위한 tz 설정
    catchup=False,
    # render Jinja template as native Python object
    render_template_as_native_obj=True,
    tags=["api_to_csv", "month", "ext"],
)
def api_to_download_kosis():

    # PostgresHook 객체 생성
    pg_hook = PostgresHook(postgres_conn_id='gsdpdb_db_conn')

    #sqlalchemy 를 이용한 connection
    engine = pg_hook.get_sqlalchemy_engine()

    # sqlalchey session 생성
    session = sessionmaker(engine, expire_on_commit=False)

    # SFTPHook 객체
    sftp_hook = SFTPHook(ssh_conn_id='ssh_inner_conn')

    @task
    def insert_collect_data_info(**kwargs):
        """
        tn_data_bsc_info 테이블에서 수집 대상 기본 정보 조회 후 th_data_clct_mastr_log 테이블에 입력
        return: collect_data_list
        """
        # 수집 대상 기본 정보 조회
        select_bsc_info_stmt = '''
                                SELECT *, (SELECT dtl_cd_nm FROM tc_com_dtl_cd WHERE group_cd = 'pvdr_site_cd' AND pvdr_site_cd = dtl_cd) AS pvdr_site_nm
                                FROM tn_data_bsc_info
                                WHERE 1=1
                                    AND LOWER(clct_yn) = 'y'
                                    AND LOWER(link_yn) = 'y'
                                    AND LOWER(link_clct_mthd_dtl_cd) = 'on_file'
                                    AND LOWER(link_clct_cycle_cd) = 'month'
                                    AND link_ntwk_otsd_insd_se = '외부'
                                    AND LOWER(pvdr_site_cd) = 'ps00010' -- 국가통계포털 (40종)
                                ORDER BY sn
                                '''
        
        data_interval_start = kwargs['data_interval_start'].in_timezone("Asia/Seoul").set(day=1)  # 처리 데이터의 시작 날짜 (데이터 기준 시점)
        data_interval_end = kwargs['data_interval_end'].in_timezone("Asia/Seoul")  # 실제 실행하는 날짜를 KST 로 설정
        # data_interval_start = kwargs['data_interval_start'].in_timezone("Asia/Seoul").add(months=-1).set(day=1) # local test
        # data_interval_end =  datetime(2025, 2, 20, tz="Asia/Seoul")  # local test
        logging.info(f"data_interval_start: {data_interval_start}, data_interval_end: {data_interval_end}")
        logging.info(f"now : {now()}")

        # 20250219 데이터 수집 2번으로 변경
        if now().strftime("%d") == '21':
            data_interval_start = now().add(months=-1) # 2025-01-19T16:21:03
            # data_interval_end = now().replace(day=7)# 2025-01-19T16:21:03.
            logging.info(f"data_interval_start222222: {data_interval_start}, data_interval_end222222: {data_interval_end}")
        collect_data_list = CommonUtil.insert_collect_data_info(select_bsc_info_stmt, session, data_interval_start, data_interval_end, kwargs)
        if collect_data_list == []:
            logging.info(f"select_collect_data_fail_info ::: 수집 대상없음 프로그램 종료")
            raise AirflowSkipException()
        return collect_data_list

    @task
    def create_directory(collect_data_list, **kwargs):
        """
        수집 파일 경로 생성
        params: tn_data_bsc_info, th_data_clct_mastr_log, tn_clct_file_info
        return: file_path: tn_clct_file_info 테이블에 저장할 파일 경로
        """
        data_interval_end = kwargs['data_interval_end'].in_timezone("Asia/Seoul")  # 실제 실행하는 날짜를 KST 로 설정
        root_collect_file_path = kwargs['var']['value'].root_collect_file_path
        file_path = CommonUtil.create_directory(collect_data_list, session, data_interval_end, root_collect_file_path, "n")
        return file_path
    
    @task
    def call_url(collect_data_list, file_path, **kwargs):
        """
        조건별 URL 설정 및 호출하여 csv 파일 생성
        params: tn_data_bsc_info, th_data_clct_mastr_log, tn_clct_file_info, file_path
        return: success_data_list
        """
        import requests
        import os
        import csv
        import re
        from util.call_url_util import CallUrlUtil

        success_data_list = []
        for collect_data_dict in collect_data_list:
            th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
            tn_data_bsc_info = TnDataBscInfo(**collect_data_dict['tn_data_bsc_info'])
            tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
            log_full_file_path = collect_data_dict['log_full_file_path']
            root_collect_file_path = kwargs['var']['value'].root_collect_file_path

            dtst_cd = th_data_clct_mastr_log.dtst_cd.lower()
            link_se_cd = tn_data_bsc_info.link_se_cd.lower()
            pvdr_site_cd = tn_data_bsc_info.pvdr_site_cd.lower()
            pvdr_inst_cd = tn_data_bsc_info.pvdr_inst_cd.lower()
            base_url = return_url = tn_data_bsc_info.link_data_clct_url

            # 파라미터 및 파라미터 길이 설정
            data_interval_start = kwargs['data_interval_start'].in_timezone("Asia/Seoul").set(day=1)  # 처리 데이터의 시작 날짜 (데이터 기준 시점)
            data_interval_end = kwargs['data_interval_end'].in_timezone("Asia/Seoul")  # 실제 실행하는 날짜를 KST 로 설정
            params_dict, params_len = CallUrlUtil.set_params(tn_data_bsc_info, session, data_interval_start, data_interval_end, kwargs)

            repeat_num = 1  # 파라미터 길이만큼 반복 호출 횟수
            page_no = 1  # 현재 페이지
            
            source_file_name = tn_clct_file_info.insd_file_nm + "." + tn_data_bsc_info.pvdr_sou_data_pvsn_stle  # 원천 파일명
            full_file_path = root_collect_file_path + file_path
            full_file_name = full_file_path + source_file_name
            copy_file_name = full_file_path + tn_clct_file_info.insd_file_nm + "_xls_sample.csv"  # 미리보기 파일
            file_size = 0  # 파일 사이즈

            try:
                # url 설정
                # return_url = f"{base_url}{CallUrlUtil.set_url(dtst_cd, pvdr_site_cd, pvdr_inst_cd, params_dict, repeat_num, page_no)}"
                return_url = f"{base_url}{CallUrlUtil.set_url(dtst_cd, link_se_cd, pvdr_site_cd, pvdr_inst_cd, params_dict, repeat_num, page_no)}"
                
                # url 호출
                response = requests.get(return_url, verify= False)                    
                response_code = response.status_code

                # 기존 파일 존재 시 삭제
                if os.path.exists(full_file_name):
                    os.remove(full_file_name)
                logging.info(f"호출 url: {return_url}")

                if response_code == 200:
                    with open(full_file_name, "wb") as file:
                        file.write(response.content)
                    
                    # 미리보기 파일 생성
                        with open(copy_file_name, 'w', newline='\n', encoding='utf-8-sig') as csvfile:
                            csv_writer = csv.writer(csvfile, delimiter=tn_data_bsc_info.link_file_sprtr)
                            content_str = response.content.decode('utf-8')
                            clean_data = content_str.replace('\t', '').replace('\n', '').replace('\r', '').replace("\r\n"," ")

                            # 헤더, 데이터 파싱
                            pattern = r'(<Row ss:AutoFitHeight="0" ss:Height="12">)|<Data ss:Type=".*?">(.*?)</Data>'
                            matches = re.findall(pattern, clean_data)

                            row_data = []
                            for match in matches:
                                # 행 구분
                                if match[0].startswith('<Row ss:AutoFitHeight="0" ss:Height="12">'):
                                    if row_data:  # 한 줄씩 쓰기
                                        csv_writer.writerow(row_data)
                                    row_data = []  # 새로운 행
                                # 열 데이터 추가
                                else:
                                    row_data.append(match[1])

                            # 마지막 행 데이터 쓰기
                            if row_data:
                                csv_writer.writerow(row_data)

                        logging.info(f"미리보기 파일 생성::: {copy_file_name}")

                else:
                    logging.info(f"call_url response_code::: {response_code}")
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP_NO_DATA, "n")

                # 파일 사이즈 확인
                if os.path.exists(full_file_name):
                    file_size = os.path.getsize(full_file_name)
                logging.info(f"call_url file_name::: {source_file_name}, file_size::: {file_size}")

                if file_size == 0:
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP_NO_DATA, "n")
                else:
                    # tn_clct_file_info 수집파일정보
                    tn_clct_file_info = CommonUtil.set_file_info(TnClctFileInfo(), th_data_clct_mastr_log, tn_clct_file_info.insd_file_nm, file_path, tn_data_bsc_info.link_file_extn, file_size, None)

                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_COMP, CONST.MSG_CLCT_COMP, "n")
                    success_data_list.append({
                        "tn_data_bsc_info" : tn_data_bsc_info.as_dict()
                        , "th_data_clct_mastr_log": th_data_clct_mastr_log.as_dict()
                        , "tn_clct_file_info": tn_clct_file_info.as_dict()
                        , "log_full_file_path" : log_full_file_path
                        })
                    CommonUtil.update_file_info_table(session, th_data_clct_mastr_log, tn_clct_file_info, tn_clct_file_info.insd_file_nm, file_path, tn_clct_file_info.insd_file_extn, file_size)
            except Exception as e:
                logging.info(f"call_url Exception::: {e}")
                CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_CLCT, CONST.STTS_ERROR, CONST.MSG_CLCT_ERROR_CALL, "n")
        return success_data_list
    
    @task
    def encrypt_zip_file(success_data_list, file_path, **kwargs):
        """
        파일 압축 및 암호화
        params: success_data_list, file_path
        return: encrypt_file
        """
        tn_data_bsc_info = TnDataBscInfo(**success_data_list[0]['tn_data_bsc_info'])

        try:
            with session.begin() as conn:
                for collect_data_dict in success_data_list:
                    th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                    th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                    tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                    log_full_file_path = collect_data_dict['log_full_file_path']
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_WORK, CONST.MSG_FILE_INSD_SEND_WORK, "n")
        
            pvdr_site_nm = tn_data_bsc_info.pvdr_site_nm
            link_file_extn = tn_data_bsc_info.link_file_extn
            pvdr_sou_data_pvsn_stle = tn_data_bsc_info.pvdr_sou_data_pvsn_stle
            encrypt_key = kwargs['var']['value'].encrypt_key
            root_collect_file_path = kwargs['var']['value'].root_collect_file_path
            full_file_path = root_collect_file_path + file_path

            FileUtil.zip_file(full_file_path, pvdr_site_nm, link_file_extn, pvdr_sou_data_pvsn_stle)
            encrypt_file = FileUtil.encrypt_file(full_file_path, pvdr_site_nm, encrypt_key, pvdr_sou_data_pvsn_stle)
        except Exception as e:
            for collect_data_dict in success_data_list:
                th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                log_full_file_path = collect_data_dict['log_full_file_path']
                CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_ERROR, CONST.MSG_FILE_INSD_SEND_ERROR_FILE, "n")
            logging.info(f"encrypt_zip_file Exception::: {e}")
            raise AirflowSkipException()
        return {
                "file_path" : file_path,
                "encrypt_file" : encrypt_file
                }

    @task
    def put_file_sftp(success_data_list, encrypt_file_path, **kwargs):
        """
        원격지 서버로 sftp 파일전송
        params: success_data_list, encrypt_file_path
        """
        file_path = encrypt_file_path['file_path']
        encrypt_file = encrypt_file_path['encrypt_file']
        root_collect_file_path = kwargs['var']['value'].root_collect_file_path
        full_file_path = root_collect_file_path + file_path

        local_filepath = full_file_path + encrypt_file
        remote_filepath = kwargs['var']['value'].final_file_path + file_path

        with session.begin() as conn:
            try:
                if not sftp_hook.path_exists(remote_filepath):
                    sftp_hook.create_directory(remote_filepath)
                sftp_hook.store_file(remote_filepath + encrypt_file, local_filepath)
                for collect_data_dict in success_data_list:
                    th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                    th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                    tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                    log_full_file_path = collect_data_dict['log_full_file_path']
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_COMP, CONST.MSG_FILE_INSD_SEND_COMP_EXT, "n")
            except Exception as e:
                for collect_data_dict in success_data_list:
                    th_data_clct_mastr_log = ThDataClctMastrLog(**collect_data_dict['th_data_clct_mastr_log'])
                    th_data_clct_mastr_log = conn.get(ThDataClctMastrLog, th_data_clct_mastr_log.clct_log_sn)
                    tn_clct_file_info = TnClctFileInfo(**collect_data_dict['tn_clct_file_info'])
                    log_full_file_path = collect_data_dict['log_full_file_path']
                    CommonUtil.update_log_table(log_full_file_path, tn_clct_file_info, session, th_data_clct_mastr_log, CONST.STEP_FILE_INSD_SEND, CONST.STTS_ERROR, CONST.MSG_FILE_INSD_SEND_ERROR_TRANS_EXT, "n")
                logging.info(f"put_file_sftp Exception::: {e}")
                raise e
    
    collect_data_list = insert_collect_data_info()
    file_path = create_directory(collect_data_list)
    success_data_list = call_url(collect_data_list, file_path)
    encrypt_file_path = encrypt_zip_file(success_data_list, file_path)
    file_path >> success_data_list >> encrypt_file_path >> put_file_sftp(success_data_list, encrypt_file_path)

dag_object = api_to_download_kosis()

# only run if the module is the main program
if __name__ == "__main__":
    conn_path = "../connections_minio_pg.yaml"
    # variables_path = "../variables.yaml"
    dtst_cd = ""

    dag_object.test(
        execution_date=datetime(2025,2,19,15,00),
        conn_file_path=conn_path,
        # variable_file_path=variables_path,
        # run_conf={"dtst_cd": dtst_cd},
    )